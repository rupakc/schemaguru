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
from dataclasses import replace
from datetime import datetime

import schemaguru as sg
from schemaguru.pii.mask import mask_free_text
from schemaguru.pii.sweep import sweep_detail
from schemaguru.render.table_block import predicate_text
from schemaguru.store.sqlite import SEARCH_BACKEND_KEY

SG_VERSION = sg.__version__
_CODE = re.compile(r"^(SG\d{3})\b")
_state = {"guru": None, "base": None, "user": None}
PROFILES = ("agent", "planner", "verifier", "router")
MAX_QUESTION, MAX_MASK_TEXT, MAX_OVERLAY = 500, 10_000, 20_000
MASK_MODES = ("masked", "none")


def _error(code, message):
    # ``generic``: the code is this glue's or a Python exception's, not a SchemaGuru
    # diagnostic code (SGnnn), so the page must not present it as one
    text = f"{code} {message}"
    generic = not _CODE.match(code)
    return json.dumps(
        {"error": {"code": code, "message": message, "generic": generic}, "text": text}
    )


def _envelope(exc):
    message = str(exc)
    code = getattr(exc, "code", None)
    if not isinstance(code, str) or not code:
        m = _CODE.match(message)
        code = m.group(1) if m else type(exc).__name__
    if message.startswith(code + " "):
        message = message[len(code) + 1 :]
    return _error(code, message)


def _check_question(question):
    if not isinstance(question, str) or not question.strip():
        return _error("empty", "the question is empty")
    if len(question) > MAX_QUESTION:
        return _error("too_large", f"the question is over {MAX_QUESTION} characters")
    return None


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
        if manifest.get("schemaguru_version") != SG_VERSION:
            return _error(
                "version_mismatch",
                f"the store was built by schemaguru {manifest.get('schemaguru_version')}, "
                f"this wheel is {SG_VERSION}",
            )
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
        _state["base"] = os.path.join(os.path.dirname(store_path), manifest["overlay"])
        _state["user"] = os.path.join(os.path.dirname(store_path), "user_overlay.yaml")
        guru.load_overlays(_state["base"])
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
    bad = _check_question(question)
    if bad:
        return bad
    if profile not in PROFILES:
        return _error("bad_profile", f"profile must be one of {', '.join(PROFILES)}")
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
        out = _guru().handle_tool_call(name, args)
        err = out.get("error")
        if isinstance(err, dict) and "generic" not in err:
            err["generic"] = not _CODE.match(str(err.get("code", "")))
        return json.dumps(out)
    except Exception as exc:
        return _envelope(exc)


def sg_path(a, b):
    """The library's own find_join_path output. The library has no public renderer for a path's
    SQL (only context views carry snippets), so none is made up here: ``sql`` stays null and
    ``sql_note`` tells the page where snippets appear."""
    out = json.loads(_tool("find_join_path", {"from": a, "to": b}))
    if "error" not in out:
        out["sql"] = None
        out["sql_note"] = "SQL snippets appear in the context view"
    return json.dumps(out)


def sg_lineage(ref, direction="upstream", depth=2):
    return _tool("lineage", {"ref": ref, "direction": direction, "depth": int(depth)})


def sg_mask(text, mode="masked"):
    if mode not in MASK_MODES:
        return _error("bad_mode", f"mode must be one of {', '.join(MASK_MODES)}")
    if not isinstance(text, str) or not text.strip():
        return _error("empty", "the text is empty")
    if len(text) > MAX_MASK_TEXT:
        return _error("too_large", f"the text is over {MAX_MASK_TEXT} characters")
    try:
        # ``categories`` is the sweep's own list of what it found; person names (the rest of
        # mask_free_text's hits) and quoted literals (none mode) are counted apart, so the page
        # never shows a glue-side count as a library category
        masked, hits = mask_free_text(text)
        swept = sweep_detail(text)[1]
        names = hits - len(swept)
        literals = 0
        if mode == "none":
            masked, literals = predicate_text(masked, "none")
            hits += literals
        return json.dumps(
            {
                "mode": mode,
                "text": masked,
                "hits": hits,
                "categories": sorted(set(swept)),
                "names": names,
                "literals": literals,
                "table_block": _table_block(mode),
            }
        )
    except Exception as exc:
        return _envelope(exc)


def _table_block(mode):
    """``describe_table shop.customers`` under ``sampling.llm_values=mode`` (config restored)."""
    guru = _guru()
    cfg = guru.config
    guru.config = replace(cfg, sampling=replace(cfg.sampling, llm_values=mode))
    try:
        return guru.handle_tool_call("describe_table", {"table": "shop.customers"})["text"]
    finally:
        guru.config = cfg


def _view(question):
    v = _guru().context(question)
    return {"text": v.text, "fingerprint": v.fingerprint}


def sg_override(overlay_yaml, question):
    """Load a user overlay on top of the base one; ephemeral, ``sg_reset`` drops it."""
    bad = _check_question(question)
    if bad:
        return bad
    if not isinstance(overlay_yaml, str) or len(overlay_yaml) > MAX_OVERLAY:
        return _error("too_large", f"the overlay is over {MAX_OVERLAY} characters")
    try:
        guru = _guru()
        # the base overlay's own findings are not the user's: report only what the YAML adds
        known = {(d.code, d.message) for d in guru.load_overlays(_state["base"])}
        before = _view(question)
        with open(_state["user"], "w", encoding="utf-8") as f:
            f.write(overlay_yaml)
        diags = guru.load_overlays(_state["base"], _state["user"])
        after = _view(question)
        return json.dumps(
            {
                "diagnostics": [
                    {"code": d.code, "severity": d.severity.value, "message": d.message}
                    for d in diags
                    if (d.code, d.message) not in known
                ],
                "before": before,
                "after": after,
            }
        )
    except Exception as exc:
        return _envelope(exc)


def sg_reset(question):
    bad = _check_question(question)
    if bad:
        return bad
    try:
        _guru().load_overlays(_state["base"])
        return json.dumps(_view(question))
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
        q = manifest["questions"][0]
        masked = _ok(sg_mask(manifest["privacy_examples"][0]["text"], "none"), "sg_mask")
        if not masked["table_block"]:
            raise RuntimeError("sg_mask: no table block")
        first = _ok(sg_reset(q), "sg_reset")["fingerprint"]
        over = _ok(sg_override(manifest["override_example"], q), "sg_override")
        if over["after"]["fingerprint"] == over["before"]["fingerprint"]:
            raise RuntimeError("sg_override: the override changed nothing")
        if _ok(sg_reset(q), "sg_reset")["fingerprint"] != first:
            raise RuntimeError("sg_reset: the base view was not restored")
        return f"ok {SG_VERSION} {opened['search_backend']}"
    finally:
        if _state["guru"] is not None:
            _state["guru"].close()
            _state["guru"] = None
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    print(smoke(sys.argv[1] if len(sys.argv) > 1 else "."))
