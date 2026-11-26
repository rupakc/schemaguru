"""The wiki's checkable claims follow the source (spec 23.3).

``test_wiki_links.py`` checks the wiki's shape; this checks facts a reader acts on, derived from
the code at test time, so a change to a default, a CLI command or an extension protocol fails
here until the wiki says the same:

* ``Configuration.md`` states the current default of each listed ``Config`` field in its table
  row (``| `field` | `default` |``);
* ``CLI.md`` lists every ``schemaguru`` subcommand in its command table;
* each ``Extending-*`` page names every method of its protocol.
"""

from __future__ import annotations

import argparse
import dataclasses
import re
from pathlib import Path
from typing import Any

import pytest

from schemaguru._util.tokens import TokenCounter
from schemaguru.catalog.protocol import CatalogReader
from schemaguru.cli.main import _parser
from schemaguru.config import Budget, Config, SamplingConfig
from schemaguru.draft.provider import LLMProvider
from schemaguru.feedback.classifiers import FailureClassifier
from schemaguru.pii.detectors import Detector
from schemaguru.refresh.classify import ChangeClassifier
from schemaguru.render.profiles import Profile
from schemaguru.scope.embeddings import Embedder
from schemaguru.scope.retrievers import Retriever
from schemaguru.server.auth import Authenticator
from schemaguru.store.protocol import Store

WIKI = Path(__file__).resolve().parent.parent.parent / "docs" / "wiki"

# (config class, field) whose default the Configuration page must state
CONFIG_FIELDS: tuple[tuple[type, str], ...] = (
    (Config, "agent_checks"),
    (Config, "measure_on_demand"),
    (Config, "block_timeout_s"),
    (Config, "fact_ttl_days"),
    (Config, "enum_max_distinct"),
    (Config, "llm_model"),
    (Budget, "max_bytes_per_probe"),
    (Budget, "max_bytes_total"),
    (Budget, "period_s"),
    (SamplingConfig, "llm_values"),
    (SamplingConfig, "store_raw_samples"),
    (SamplingConfig, "measurement"),
    (SamplingConfig, "sample_threshold_bytes"),
    (SamplingConfig, "sample_threshold_rows"),
    (SamplingConfig, "exact_key_limit"),
)

PROTOCOL_PAGES: dict[str, tuple[type, ...]] = {
    "Extending-CatalogReader": (CatalogReader,),
    "Extending-Store": (Store,),
    "Extending-Retriever-and-Embedder": (Retriever, Embedder),
    "Extending-TokenCounter": (TokenCounter,),
    "Extending-LLMProvider": (LLMProvider,),
    "Extending-Detector": (Detector,),
    "Extending-FailureClassifier": (FailureClassifier,),
    "Extending-Profile-and-Format": (Profile,),
    "Extending-ChangeClassifier": (ChangeClassifier,),
    "Extending-Authenticator": (Authenticator,),
}


def renderings(value: Any) -> set[str]:
    """Ways a default may be written in a table cell (without the backticks)."""
    if isinstance(value, str):
        return {f'"{value}"'}
    if value is None or isinstance(value, bool):
        return {repr(value)}
    if isinstance(value, float):
        return {repr(value)}
    out = {str(value), f"{value:_}"}
    for shift in range(10, 41, 10):
        base, rest = divmod(value, 1 << shift)
        if base and not rest:
            out.add(f"{base} << {shift}")
    return out


def default_of(cls: type, name: str) -> Any:
    f = next(f for f in dataclasses.fields(cls) if f.name == name)  # type: ignore[arg-type]
    return f.default


def stated_default(text: str, name: str) -> str | None:
    """The second cell of the table row for ``name``, without backticks."""
    m = re.search(rf"^\| `{re.escape(name)}` \| `([^`]*)`[^|]*\|", text, re.M)
    return None if m is None else m.group(1)


def missing_members(text: str, protocol: type) -> list[str]:
    names = [
        n
        for n, v in vars(protocol).items()
        if not n.startswith("_") and (callable(v) or isinstance(v, property))
    ]
    forms = ("`{}`", "`{}(", "def {}(")
    return [n for n in names if not any(f.format(n) in text for f in forms)]


def subcommands() -> list[str]:
    for action in _parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(action.choices)
    raise AssertionError("the CLI parser has no subcommands")


def _read(page: str) -> str:
    return (WIKI / f"{page}.md").read_text(encoding="utf-8")


# ----- negative controls ----------------------------------------------------------------------


def test_negative_control_config_default() -> None:
    row = '| `agent_checks` | `"deny"` | what it does |\n'
    assert stated_default(row, "agent_checks") == '"deny"'
    assert stated_default(row, "agent_checks") not in renderings("allow")  # a flipped default
    assert stated_default(row, "period_s") is None
    assert "50 << 30" in renderings(50 << 30) and "500_000_000" in renderings(500_000_000)


def test_negative_control_protocol_members() -> None:
    class P:
        def run(self) -> None: ...

        def stop(self) -> None: ...

    assert missing_members("calls `run` and `stop`", P) == []
    assert missing_members("calls `run` only", P) == ["stop"]


# ----- the wiki ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("cls", "name"), CONFIG_FIELDS, ids=[n for _, n in CONFIG_FIELDS])
def test_configuration_states_current_default(cls: type, name: str) -> None:
    stated = stated_default(_read("Configuration"), name)
    assert stated is not None, f"Configuration.md has no table row for `{name}`"
    assert stated in renderings(default_of(cls, name)), (
        f"Configuration.md says {name} defaults to {stated}; the code says "
        f"{default_of(cls, name)!r}"
    )


def test_cli_page_lists_every_subcommand() -> None:
    text = _read("CLI")
    missing = [c for c in subcommands() if f"| `{c}" not in text]
    assert not missing, missing


@pytest.mark.parametrize("page", sorted(PROTOCOL_PAGES))
def test_extending_page_names_protocol_methods(page: str) -> None:
    text = _read(page)
    missing = {p.__name__: missing_members(text, p) for p in PROTOCOL_PAGES[page]}
    assert all(not v for v in missing.values()), missing


def test_sampling_thresholds_stated() -> None:
    """Both "large table" thresholds are stated with when each applies: catalog bytes when
    known, the row count only when bytes are unknown (``measure/sampling.py``)."""
    wiki = " ".join(_read("Sampling").split())
    assert "50 GiB by catalog bytes, or, only when bytes are unknown, 500 million rows" in wiki
    guide = " ".join((WIKI.parent / "guide" / "sampling.md").read_text(encoding="utf-8").split())
    assert "`bytes ≥ sample_threshold_bytes` (50 GiB), or, with bytes unknown," in guide
    assert "`row_count ≥ sample_threshold_rows` (500M)" in guide


def test_wiki_sync_explains_a_missing_wiki_repository() -> None:
    """The wiki sync fails with an actionable error when ``<repo>.wiki.git`` does not exist yet
    (GitHub creates it only with the first page), instead of a bare ``git clone`` failure."""
    wf = (WIKI.parent.parent / ".github" / "workflows" / "wiki.yml").read_text(encoding="utf-8")
    step = wf.split("- name: Clone the wiki repository", 1)[1].split("- name:", 1)[0]
    assert 'if ! git -c http.extraheader="$auth" clone "$WIKI_URL" wiki; then' in step
    assert "::error::Cannot clone $WIKI_URL" in step
    assert "enable the wiki" in step and "create any page once" in step
    assert "exit 1" in step


def test_bigquery_region_documented() -> None:
    """The BigQuery page states the reader's default region and how to set another one, derived
    from the constructor (negative control: the same check on a page that omits them fails)."""
    import inspect

    from schemaguru.catalog.readers.bigquery import BigQueryReader

    default = inspect.signature(BigQueryReader).parameters["region"].default

    def documents(text: str) -> bool:
        return f'`region` defaults to `"{default}"`' in text and "region=" in text

    assert documents(_read("Warehouse-BigQuery"))
    assert not documents("Pass `project=` to the reader.")
    assert not documents('`region` defaults to `"eu"`; set `region=`')
