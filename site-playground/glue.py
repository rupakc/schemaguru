"""The playground's Python side, run by Pyodide after the schemaguru and factlang wheels are
unpacked and the store, manifest and base overlay are written into its file system.

Each ``sg_*`` function takes plain values and returns a JSON string for the page's script. A
failure returns the tool layer's error shape, ``{"error": {"code", "message"}, "text"}``,
never raises. Nothing here connects to a warehouse: the store was crawled, measured and its
view lineage parsed at build time (``scripts/build_playground_store.py``); this file serves it
offline. docs.yml runs it against the freshly built wheels in a clean venv (no extras:
``python -I glue.py <playground dir>``), and tests/conformance/test_playground.py runs it
against the source tree.
"""

import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime

import schemaguru as sg
from schemaguru.pii.mask import mask_free_text
from schemaguru.store.sqlite import SEARCH_BACKEND_KEY

SG_VERSION = sg.__version__
_CODE = re.compile(r"^(SG\d{3})\b")
_state = {"guru": None}


def _envelope(exc):
    message = str(exc)
    code = getattr(exc, "code", None)
    if not isinstance(code, str) or not code:
        m = _CODE.match(message)
        code = m.group(1) if m else type(exc).__name__
    if message.startswith(code + " "):
        message = message[len(code) + 1 :]
    return json.dumps({"error": {"code": code, "message": message}, "text": f"{code} {message}"})


def _guru():
    guru = _state["guru"]
    if guru is None:
        raise RuntimeError("the playground store is not open (call sg_open first)")
    return guru


def _backend(guru):
    return (guru.store.crawl_state_get(SEARCH_BACKEND_KEY) or {}).get("backend")


def sg_open(store_path, manifest_json):
    """Open the store offline and load the base overlay that sits next to it."""
    try:
        manifest = json.loads(manifest_json)
        now = datetime.fromisoformat(manifest["now"])
        if _state["guru"] is not None:
            _state["guru"].close()
            _state["guru"] = None
        guru = sg.Guru(
            store=store_path,
            connector=None,
            dialect="duckdb",
            name="shop",
            now=lambda: now,
        )
        _state["guru"] = guru
        overlay = os.path.join(os.path.dirname(store_path), manifest["overlay"])
        guru.load_overlays(overlay)
        tables = sorted(t.ref.canonical() for t in guru.store.iter_tables())
        return json.dumps(
            {
                "ok": True,
                "schemaguru": SG_VERSION,
                "search_backend": _backend(guru),
                "tables": tables,
            }
        )
    except Exception as exc:
        return _envelope(exc)


def sg_context(question, profile="agent", budget_tokens=4000):
    try:
        view = _guru().context(question, profile=profile, budget_tokens=int(budget_tokens))
        return json.dumps(
            {
                "text": view.text,
                "tokens": view.tokens,
                "tables": [t.canonical() for t in view.tables],
                "fingerprint": view.fingerprint,
                "profile": profile,
            }
        )
    except Exception as exc:
        return _envelope(exc)


def _tool(name, args):
    try:
        return json.dumps(_guru().handle_tool_call(name, args))
    except Exception as exc:
        return _envelope(exc)


def sg_path(a, b):
    return _tool("find_join_path", {"from": a, "to": b})


def sg_lineage(ref, direction="upstream", depth=2):
    return _tool("lineage", {"ref": ref, "direction": direction, "depth": int(depth)})


def sg_mask(text, mode="masked"):
    try:
        if mode != "masked":
            raise ValueError(f"unknown mask mode {mode!r}")
        masked, hits = mask_free_text(text)
        return json.dumps({"mode": mode, "text": masked, "hits": hits})
    except Exception as exc:
        return _envelope(exc)


def _ok(result, what):
    data = json.loads(result)
    if "error" in data:
        raise RuntimeError(f"{what}: {data['text']}")
    return data


def smoke(playground_dir):
    """Open a copy of the built store and exercise every call; ``ok <version> <backend>``."""
    with open(os.path.join(playground_dir, "playground.json"), encoding="utf-8") as f:
        manifest_json = f.read()
    manifest = json.loads(manifest_json)
    work = tempfile.mkdtemp(prefix="sg-playground-")
    try:
        # opening read-write switches the journal to WAL: never touch the built artifact
        for name in (manifest["store"], manifest["overlay"]):
            shutil.copyfile(os.path.join(playground_dir, name), os.path.join(work, name))
        opened = _ok(sg_open(os.path.join(work, manifest["store"]), manifest_json), "sg_open")
        if opened["schemaguru"] != manifest["schemaguru_version"]:
            raise RuntimeError(
                f"store built by {manifest['schemaguru_version']}, wheel is {SG_VERSION}"
            )
        view = _ok(sg_context(manifest["questions"][0]), "sg_context")
        if view["tokens"] <= 0:
            raise RuntimeError("sg_context: empty view")
        if _ok(sg_path("shop.payments", "shop.customers"), "sg_path")["refusal"] is not None:
            raise RuntimeError("sg_path: shop.payments -> shop.customers was refused")
        if not _ok(sg_lineage(manifest["lineage_subjects"][-3], "upstream", 3), "sg_lineage")[
            "nodes"
        ]:
            raise RuntimeError("sg_lineage: no upstream nodes")
        if _ok(sg_mask("write to jane.doe@example.org"), "sg_mask")["hits"] < 1:
            raise RuntimeError("sg_mask: the e-mail was not masked")
        return f"ok {SG_VERSION} {opened['search_backend']}"
    finally:
        if _state["guru"] is not None:
            _state["guru"].close()
            _state["guru"] = None
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    print(smoke(sys.argv[1] if len(sys.argv) > 1 else "."))
