"""A reused, killable parser worker process with hard limits (0.2.0 spec 4.4).

A single ``sqlglot`` parse cannot be interrupted, so definitions are parsed in a child
process: ``python -m schemaguru.lineage.worker``. Like the ``spawn`` start method it is a fresh
interpreter on every OS, but it never re-imports the host's ``__main__``, so a host script
without a main guard is safe. The child is started lazily, reused for every request of the
pool (start-up is paid once), and killed and replaced when a request runs past ``timeout_s``
or the child dies.

Protocol: one JSON object per line over the child's stdin/stdout; the child first writes a
``{"ready": true}`` line. Its stderr is ``DEVNULL``. The raw SQL lives only in memory: no
failure, exception or log line carries it -- a failure is a fixed :data:`FailureReason`.

The time cap is per request: a definition analysed in two ``map`` calls (extract, then
lineage) can take up to 2 x ``timeout_s``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import IO, Any, get_args

from schemaguru.lineage.dialects import sqlglot_dialect
from schemaguru.lineage.parse import (
    MAX_DEFINITION_CHARS,
    ColumnLineage,
    FailureReason,
    LineageFailure,
    ParsedLineage,
    extract_tables,
    lineage_available,
    parse_lineage,
)
from schemaguru.model.lineage import TRANSFORMS, RawName, ResolvedTable

DEFAULT_TIMEOUT_S = 5.0

# Waiting for a new child's ready line is not a definition's time: it is bounded separately.
STARTUP_TIMEOUT_S = 60.0

# The longest reply line read from a child; a longer one is out of protocol (``worker_died``).
MAX_REPLY_BYTES = 32 << 20

_MEMORY_ENV = "SCHEMAGURU_LINEAGE_MEMORY_LIMIT"
_REASONS: frozenset[str] = frozenset(get_args(FailureReason))


@dataclass(frozen=True)
class ExtractRequest:
    """List the relations ``sql`` reads (:func:`~schemaguru.lineage.extract_tables`)."""

    sql: str
    dialect: str


@dataclass(frozen=True)
class LineageRequest:
    """Parse the lineage of ``sql`` against ``schema`` (see ``parse_lineage``)."""

    sql: str
    dialect: str
    schema: Mapping[RawName, ResolvedTable]
    columns: bool = True


WorkerResult = tuple[RawName, ...] | ParsedLineage | LineageFailure


# ----- child side ------------------------------------------------------------------------------


def _write(obj: Mapping[str, Any]) -> None:
    # ASCII-only JSON: the pipe's text encoding never matters
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _limit_memory() -> None:
    raw = os.environ.get(_MEMORY_ENV)
    if not raw or not sys.platform.startswith("linux"):
        return
    import resource  # POSIX only

    limit = int(raw)
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def serve(handler: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    """The worker's line loop: announce readiness, then for each JSON line on stdin call
    ``handler`` and write its reply as one JSON line; return on EOF. A handler may write to
    ``sys.stdout`` itself."""
    _limit_memory()
    logging.getLogger("sqlglot").disabled = True  # it quotes SQL in its messages
    if lineage_available():
        import sqlglot  # noqa: F401  (paid here, not inside the first request's time cap)
    _write({"ready": True})
    for line in sys.stdin:
        try:
            request = json.loads(line)
            reply = handler(request) if isinstance(request, dict) else {"error": "parse_error"}
        except Exception:
            reply = {"error": "parse_error"}
        _write(reply)


def _encode_lineage(p: ParsedLineage) -> dict[str, Any]:
    return {
        "upstream": list(p.upstream),
        "unresolved": list(p.unresolved),
        "columns": [[c.column, list(c.inputs), c.transform, list(c.unresolved)] for c in p.columns],
    }


def handle(request: dict[str, Any]) -> dict[str, Any]:
    """The real request handler: ``{"op": "extract" | "lineage", ...}`` to ``{"tables": ...}``,
    ``{"lineage": ...}`` or ``{"error": <reason>}``."""
    sql, dialect = request["sql"], request["dialect"]
    try:
        if request["op"] == "extract":
            return {"tables": [list(t) for t in extract_tables(sql, dialect)]}
        schema = {
            (k[0], k[1], k[2]): ResolvedTable(ref, tuple(cols))
            for k, ref, cols in request["schema"]
        }
        parsed = parse_lineage(sql, dialect, schema, columns=bool(request["columns"]))
        return {"lineage": _encode_lineage(parsed)}
    except LineageFailure as exc:
        return {"error": exc.reason}


def main() -> None:
    serve(handle)


# ----- parent side -----------------------------------------------------------------------------


class _Died(Exception):
    """The child is gone or spoke out of protocol."""


def _encode(req: ExtractRequest | LineageRequest) -> bytes:
    if isinstance(req, ExtractRequest):
        obj: dict[str, Any] = {"op": "extract", "sql": req.sql, "dialect": req.dialect}
    else:
        obj = {
            "op": "lineage",
            "sql": req.sql,
            "dialect": req.dialect,
            "schema": [[list(k), t.ref, list(t.columns)] for k, t in req.schema.items()],
            "columns": req.columns,
        }
    return (json.dumps(obj) + "\n").encode("ascii")


def _strs(x: Any) -> tuple[str, ...]:
    if not isinstance(x, list) or not all(isinstance(s, str) for s in x):
        raise _Died
    return tuple(x)


def _raw_name(x: Any) -> RawName:
    if not (isinstance(x, list) and len(x) == 3 and isinstance(x[2], str)):
        raise _Died
    if not all(p is None or isinstance(p, str) for p in x[:2]):
        raise _Died
    return (x[0], x[1], x[2])


def _decode(line: bytes, op: str) -> WorkerResult:
    try:
        reply = json.loads(line)
    except ValueError:
        raise _Died from None
    if not isinstance(reply, dict):
        raise _Died
    if "error" in reply:
        reason = reply["error"]
        if reason not in _REASONS:
            raise _Died
        return LineageFailure(reason)
    if op == "extract":
        if not isinstance(reply.get("tables"), list):
            raise _Died
        return tuple(_raw_name(t) for t in reply["tables"])
    lin = reply.get("lineage")
    if not isinstance(lin, dict) or not isinstance(lin.get("columns"), list):
        raise _Died
    columns: list[ColumnLineage] = []
    for c in lin["columns"]:
        if not (isinstance(c, list) and len(c) == 4 and isinstance(c[0], str)):
            raise _Died
        if c[2] not in TRANSFORMS:
            raise _Died
        columns.append(ColumnLineage(c[0], _strs(c[1]), c[2], _strs(c[3])))
    return ParsedLineage(_strs(lin.get("upstream")), _strs(lin.get("unresolved")), tuple(columns))


def _pump(stream: IO[bytes], lines: queue.Queue[bytes | None]) -> None:
    # A reader thread: ``Queue.get(timeout=...)`` is the one wait that works on Windows pipes.
    # ``None`` ends the stream: EOF, a read error, or a line longer than MAX_REPLY_BYTES.
    limit = MAX_REPLY_BYTES
    try:
        while line := stream.readline(limit + 1):
            if len(line) > limit:
                break
            lines.put(line)
    except (OSError, ValueError):
        pass
    lines.put(None)


def _send(stdin: IO[bytes], data: bytes, failed: threading.Event) -> None:
    # A writer thread: a child that stops reading blocks this write, not the caller.
    try:
        stdin.write(data)
        stdin.flush()
    except (OSError, ValueError):  # BrokenPipeError included; ValueError: closed by a kill
        failed.set()


def _kill_and_reap(proc: subprocess.Popen[bytes]) -> None:
    """Kill ``proc`` if it still runs, reap it and close its pipes (idempotent)."""
    if proc.poll() is None:
        with contextlib.suppress(OSError):
            proc.kill()
    proc.wait()
    for stream in (proc.stdin, proc.stdout):
        if stream is not None:
            with contextlib.suppress(OSError, ValueError):
                stream.close()


class ParserPool:
    """One reused worker process, started lazily. Use as a context manager or call
    :meth:`close`; a closed pool starts a new worker on its next :meth:`map`."""

    def __init__(
        self,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_chars: int = MAX_DEFINITION_CHARS,
        memory_limit_bytes: int | None = 1 << 30,
        command: Sequence[str] | None = None,
    ) -> None:
        self._timeout_s = timeout_s
        self._max_chars = max_chars
        self._memory_limit = memory_limit_bytes
        self._command = (
            list(command)
            if command is not None
            else [sys.executable, "-m", "schemaguru.lineage.worker"]
        )
        self._proc: subprocess.Popen[bytes] | None = None
        self._reaper: weakref.finalize[Any, Any] | None = None
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._restarts = 0

    @property
    def restarts(self) -> int:
        """How many workers were killed or found dead and so replaced."""
        return self._restarts

    def __enter__(self) -> ParserPool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Stop the worker (EOF, then a kill if it does not exit) and reap it."""
        proc, self._proc = self._proc, None
        if self._reaper is not None:
            self._reaper.detach()
            self._reaper = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()

    def map(self, requests: Sequence[ExtractRequest | LineageRequest]) -> list[WorkerResult]:
        """One result per request, in input order. Never raises for a definition: a failure is
        a :class:`LineageFailure` carrying a fixed reason."""
        out: list[WorkerResult] = []
        startup_failed = False
        for req in requests:
            if len(req.sql) > self._max_chars:
                out.append(LineageFailure("too_large"))
            elif sqlglot_dialect(req.dialect) is None:
                out.append(LineageFailure("unsupported_dialect"))
            elif startup_failed:
                out.append(LineageFailure("worker_died"))
            else:
                if self._proc is not None and self._proc.poll() is not None:
                    self._discard()  # died while idle: no definition is blamed for it
                if self._proc is None and not self._start():
                    startup_failed = True  # do not pay STARTUP_TIMEOUT_S once per definition
                    out.append(LineageFailure("worker_died"))
                    continue
                out.append(self._ask(req))
        return out

    # ----- internals ---------------------------------------------------------------------------

    def _start(self) -> bool:
        env = dict(os.environ)
        env.pop(_MEMORY_ENV, None)
        if self._memory_limit is not None:
            env[_MEMORY_ENV] = str(self._memory_limit)
        try:
            proc = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        except OSError:
            self._restarts += 1
            return False
        assert proc.stdout is not None
        self._proc = proc
        # a pool that is garbage-collected or alive at interpreter exit still reaps its child
        self._reaper = weakref.finalize(self, _kill_and_reap, proc)
        self._lines = queue.Queue()
        threading.Thread(target=_pump, args=(proc.stdout, self._lines), daemon=True).start()
        try:
            ready = self._lines.get(timeout=STARTUP_TIMEOUT_S)
        except queue.Empty:
            ready = None
        try:
            ok = ready is not None and json.loads(ready) == {"ready": True}
        except ValueError:
            ok = False
        if not ok:
            self._discard()
        return ok

    def _discard(self) -> None:
        """Kill and reap the current worker; the next request starts a new one."""
        self._proc = None
        self._restarts += 1
        if self._reaper is not None:
            self._reaper()  # kill + reap; a blocked writer thread then fails with EPIPE
            self._reaper = None

    def _ask(self, req: ExtractRequest | LineageRequest) -> WorkerResult:
        """Send ``req`` and wait for its reply; writing and waiting share one deadline."""
        proc = self._proc
        assert proc is not None and proc.stdin is not None
        deadline = time.monotonic() + self._timeout_s
        failed = threading.Event()
        writer = threading.Thread(
            target=_send, args=(proc.stdin, _encode(req), failed), daemon=True
        )
        writer.start()
        writer.join(timeout=self._timeout_s)
        if writer.is_alive():  # the child stopped reading
            self._discard()
            writer.join(timeout=5)
            return LineageFailure("timeout")
        if failed.is_set():
            self._discard()
            return LineageFailure("worker_died")
        try:
            line = self._lines.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            self._discard()
            return LineageFailure("timeout")
        op = "extract" if isinstance(req, ExtractRequest) else "lineage"
        try:
            if line is None:
                raise _Died
            return _decode(line, op)
        except _Died:
            self._discard()
            return LineageFailure("worker_died")


if __name__ == "__main__":
    main()
