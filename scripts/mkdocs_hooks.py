"""MkDocs hooks: publish the playground assets, which live outside ``docs/``.

``site-playground/`` (the page script, styles and Python glue) and ``build/playground/`` (the
two wheels, the store and its manifest, built by ``scripts/build_playground_store.py`` from the
same commit) become ``assets/playground/``, with ``wheels.json`` naming the wheels the browser
installs. A missing build piece is a logged warning, so ``mkdocs build --strict`` fails rather
than shipping a playground that cannot start.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from mkdocs.structure.files import File, Files

PLAYGROUND_SRC = Path(__file__).resolve().parent.parent / "site-playground"
PLAYGROUND_BUILD = Path(__file__).resolve().parent.parent / "build" / "playground"
PLAYGROUND_DEST = "assets/playground"
# the store, its manifest and the base overlay the manifest names (``user_overlay.yaml`` is a
# leftover of the glue's smoke run, never published)
PLAYGROUND_DATA = ("playground.db", "playground.json", "base_overlay.yaml")
LANDING = Path(__file__).resolve().parent.parent / "docs" / "generated" / "landing.json"
log = logging.getLogger("mkdocs.hooks.schemaguru")


def on_files(files: Files, config: Any) -> Files:
    _publish_playground(files, config)
    return files


def _newest_wheel(name: str) -> Path | None:
    if not PLAYGROUND_BUILD.is_dir():
        return None
    wheels = sorted(
        PLAYGROUND_BUILD.glob(f"{name}-*-py3-none-any.whl"),
        key=lambda p: (p.stat().st_mtime, p.name),
    )
    return wheels[-1] if wheels else None


def _publish_playground(files: Files, config: Any) -> None:
    def publish(path: Path) -> None:
        dest = f"{PLAYGROUND_DEST}/{path.name}"
        files.append(File.generated(config, dest, abs_src_path=str(path)))

    for path in sorted(PLAYGROUND_SRC.iterdir()):
        if path.is_file() and path.suffix in {".py", ".js", ".css"}:
            publish(path)
    missing: list[str] = []
    wheels: dict[str, str | None] = {}
    for name in ("schemaguru", "factlang"):
        wheel = _newest_wheel(name)
        wheels[name] = wheel.name if wheel else None
        if wheel is None:
            missing.append(f"the {name} wheel")
        else:
            publish(wheel)
    for piece in PLAYGROUND_DATA:
        if (PLAYGROUND_BUILD / piece).is_file():
            publish(PLAYGROUND_BUILD / piece)
        else:
            missing.append(piece)
    if missing:
        log.warning(
            "%s missing from %s: the playground cannot load. Build it as CONTRIBUTING.md, "
            "section 'Docs site', describes (wheels, factlang wheel, then "
            "scripts/build_playground_store.py)",
            ", ".join(missing),
            PLAYGROUND_BUILD,
        )
    files.append(
        File.generated(config, f"{PLAYGROUND_DEST}/wheels.json", content=json.dumps(wheels))
    )


def on_env(env: Any, config: Any, files: Any) -> Any:
    """Hand the generated landing data to the templates (``landing``); warn if it is missing."""
    if not LANDING.is_file():
        log.warning("%s is missing; run python scripts/gen_docs.py", LANDING)
        return env
    env.globals["landing"] = json.loads(LANDING.read_text(encoding="utf-8"))
    return env
