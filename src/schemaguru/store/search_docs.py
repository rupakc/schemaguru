"""What feeds a table's search document, shared by the SQLite and Postgres stores (spec 7.2).

A table's document holds its name tokens, column tokens, comments, the text of its meaning
facts (table- and column-level) and the glossary terms that map to it. When a fact is written or
deleted, :func:`doc_tables` names the documents to rebuild.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any

from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef

MEANING_KINDS = ("DESCRIPTION", "USE_CASES", "ENTITY_TYPE", "PITFALL")
GLOSSARY = "GLOSSARY"  # a term is indexed into the docs of the tables it maps_to
INDEXED_KINDS = frozenset((*MEANING_KINDS, GLOSSARY))


def strings(obj: Any) -> Iterator[str]:
    """Every string inside a JSON-like value, in order."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, Mapping):
        for v in obj.values():
            yield from strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from strings(v)


def _map_table(raw: object) -> str | None:
    """The table key of one glossary ``maps_to`` entry (a ref dict or canonical text)."""
    try:
        if isinstance(raw, Mapping):
            return ObjectRef.from_dict(dict(raw)).table_ref().canonical()
        if isinstance(raw, str):
            return ObjectRef.parse(raw, has_column=False).canonical()
    except (KeyError, TypeError, ValueError):
        return None
    return None


def glossary_tables(value: Mapping[str, Any]) -> set[str]:
    """The table keys a glossary term's ``maps_to`` names (unparseable entries are skipped)."""
    maps = value.get("maps_to")
    found = (_map_table(m) for m in maps) if isinstance(maps, (list, tuple)) else ()
    return {t for t in found if t is not None}


def doc_tables(kind: str, subject: ObjectRef | JoinRef, value: Mapping[str, Any]) -> set[str]:
    """The tables whose search document a fact feeds: its own table for a meaning kind, every
    ``maps_to`` table for a glossary term, none otherwise."""
    if kind in MEANING_KINDS and isinstance(subject, ObjectRef):
        return {subject.table_ref().canonical()}
    if kind == GLOSSARY:
        return glossary_tables(value)
    return set()


def stored_doc_tables(data: str) -> set[str]:
    """:func:`doc_tables` of a stored fact row (its JSON text)."""
    d = json.loads(data)
    subj = d["subject"]
    subject = JoinRef.from_dict(subj) if "left" in subj else ObjectRef.from_dict(subj)
    return doc_tables(d["kind"], subject, d.get("value") or {})
