"""Credential redaction for messages (layer 0): connection URLs and ``key=value`` secrets.

Shared by the CLI, the store opener, the agent surface and stored error facts, so a password
never reaches a terminal, a tool result, a log line or the store.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from urllib.parse import urlsplit

_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*")
_FILE_SCHEMES = frozenset({"duckdb", "sqlite"})  # a local file path: shown, never a secret

# ``key=value`` / ``key: value`` credentials in free text (ODBC strings, driver messages,
# config echoes). The key may be glued to a word (``db_password``, ``x_secret``); a generic
# ``token`` only counts with ``=`` (a parser's "unexpected token: FROM" is not a secret).
_KV_KEYS = r"password|passwd|pwd|secret|api[_-]?key|access[_-]?key|private[_-]?key"
_KV_VALUE = r"(?:\"[^\"]*\"|'[^']*'|[^\s;&,'\")\]}]+)"
_KV_SECRET = re.compile(
    rf"(?<![A-Za-z0-9])((?:[A-Za-z0-9]+[_-])*(?:{_KV_KEYS}))(\s*[:=]\s*){_KV_VALUE}"
    rf"|(?<![A-Za-z0-9])((?:[A-Za-z0-9]+[_-])*token)(\s*=\s*){_KV_VALUE}"
    r"|(?<![A-Za-z0-9])(bearer)(\s+)[A-Za-z0-9._~+/=-]{8,}",
    re.IGNORECASE,
)


def _kv_sub(m: re.Match[str]) -> str:
    key = m.group(1) or m.group(3) or m.group(5)
    sep = m.group(2) or m.group(4) or m.group(6)
    return f"{key}{sep}***"


def scrub_credentials(text: str) -> str:
    """``text`` with the value of every ``password=``/``pwd=``/``secret:``/``api_key=``/
    ``token=``/``Bearer`` credential replaced by ``***`` (any value length)."""
    return _KV_SECRET.sub(_kv_sub, text)


def redact_url(url: str) -> str:
    """What may be shown of a connection URL in an error, log or repr: fails CLOSED.

    Only ``scheme://host[:port]``, and only when the URL parses cleanly with at most one ``@``
    and that ``@`` inside the authority; anything else is ``scheme://***`` (or ``***`` without
    a scheme). Userinfo, path, query and fragment are never shown -- except the file path of a
    local database (``duckdb://`` / ``sqlite://`` with no userinfo), which is not a secret and
    is what a user needs to see; its query (which can carry a token) is still dropped.
    """
    scheme, sep, _ = url.partition("://")
    if not sep or not _SCHEME.fullmatch(scheme):
        return "***"
    closed = f"{scheme}://***"
    if scheme.split("+", 1)[0].lower() in _FILE_SCHEMES:
        local = url.split("?", 1)[0].split("#", 1)[0]
        authority = local[len(scheme) + 3 :].split("/", 1)[0]
        if "@" not in url and ":" not in authority:
            return local
        return closed
    if url.count("@") > 1:
        return closed
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return closed
    if "@" in url and "@" not in parts.netloc:
        return closed  # the '@' sits in a path, query or fragment: the authority was misread
    if host is None:
        return closed if parts.netloc else f"{scheme}://"
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", host):
        return closed
    shown = f"[{host}]" if ":" in host else host
    return f"{scheme}://{shown}" + (f":{port}" if port is not None else "")


_URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")


def redact_text(text: str) -> str:
    """``text`` with every embedded ``scheme://...`` token replaced by :func:`redact_url`.

    For messages that may quote a URL (errors, logs, CLI output). A token runs to the next
    whitespace, so a URL is judged whole; text without a URL is unchanged.
    """
    return _URL_IN_TEXT.sub(lambda m: redact_url(m.group(0)), text)


_URL_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
_TRAILING = "'\")>]},.;:"


def scrub_text(text: str, known: Sequence[str | None] = ()) -> str:
    """``text`` with credentials removed from every URL in it and the rest left intact.

    The exact strings in ``known`` (the ``--url`` value) are replaced by their redacted form
    first; then every URL-shaped token is redacted, then every ``key=value`` credential
    (:func:`scrub_credentials`). Never redacts a whole message.
    """
    for url in sorted({u for u in known if u}, key=len, reverse=True):
        text = text.replace(url, redact_url(url))
    parts = re.split(r"(\s+)", text)  # tokens at even indexes, whitespace at odd ones
    out: list[str] = []
    i = 0
    while i < len(parts):
        tok = parts[i]
        m = _URL_TOKEN.search(tok)
        if m is None:
            out.append(tok)
            i += 1
            continue
        body = m.group(0).rstrip(_TRAILING)
        authority = body.partition("://")[2].split("/", 1)[0]
        if "@" not in body and re.search(r":\D", authority):
            # userinfo with a space in the password: the '@' is in a later token (fail closed)
            j = i + 2
            while j < len(parts) and j <= i + 16 and "://" not in parts[j]:
                if "@" in parts[j]:
                    out.append(tok[: m.start()] + body.split("://")[0] + "://***")
                    i = j + 1
                    break
                j += 2
            else:
                out.append(tok[: m.start()] + redact_url(body) + tok[m.start() + len(body) :])
                i += 1
            continue
        out.append(tok[: m.start()] + redact_url(body) + tok[m.start() + len(body) :])
        i += 1
    return scrub_credentials("".join(out))
