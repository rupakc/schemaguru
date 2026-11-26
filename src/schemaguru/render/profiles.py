"""Profiles: one rendering per role (spec 13.2). The set is open (``register_profile``).

* ``agent`` (default): executor-style detail, the spec 13.3 block.
* ``planner``: tables, use cases, keys and join targets; few columns.
* ``verifier``: keyed, parseable lines (``TABLE``, ``WARN``, ``PK``, ``COLUMNS``, ``VALUES``...).
* ``router``: one line per table (name and description), for classification.

Every built-in puts warnings first (spec 25.13).
"""

from __future__ import annotations

from typing import Protocol

from schemaguru import plugins
from schemaguru.model.detail import DetailLevel
from schemaguru.pii.policy import ValuePolicy
from schemaguru.render.knowledge import TableKnowledge
from schemaguru.render.legend import WARN
from schemaguru.render.table_block import (
    RANK,
    STANDARD_UP,
    analyse,
    ident,
    name_only,
    outbound,
    render_agent,
    shown_columns,
    warning_lines,
)


class Profile(Protocol):
    def render_table(self, eff: TableKnowledge, level: DetailLevel) -> str: ...


def finish(eff: TableKnowledge, text: str) -> str:
    """The last step of every built-in profile: the outbound shape sweep (spec 11)."""
    return outbound(text, ValuePolicy.for_table(eff.ref, eff.config.sampling))[0]


class AgentProfile:
    def render_table(self, eff: TableKnowledge, level: DetailLevel) -> str:
        return finish(eff, render_agent(eff, level))


class PlannerProfile:
    def render_table(self, eff: TableKnowledge, level: DetailLevel) -> str:
        return finish(eff, self._render(eff, level))

    def _render(self, eff: TableKnowledge, level: DetailLevel) -> str:
        a = analyse(eff)
        if level is DetailLevel.NAME_ONLY:
            return name_only(a)
        r = RANK[level]
        lines = [f"[{a.title}]" + (f" {a.description}" if a.description else "")]
        lines += warning_lines(a)
        lines += [f"  {b}" for b in a.built_from]
        if a.answers:
            lines.append(f"  {a.answers}")
        if a.rows and r <= STANDARD_UP:
            lines.append(f"  {a.rows}")
        if a.keys:
            lines.append("  Keys: " + "; ".join(a.keys))
        if a.live_rows:
            lines.append(f"  {a.live_rows}")
        return "\n".join(lines)


class VerifierProfile:
    def render_table(self, eff: TableKnowledge, level: DetailLevel) -> str:
        return finish(eff, self._render(eff, level))

    def _render(self, eff: TableKnowledge, level: DetailLevel) -> str:
        a = analyse(eff)
        lines = [f"TABLE {a.title}"]
        if a.never_query is not None:
            lines.append(f"NEVER_QUERY {a.never_query}")
        if level is DetailLevel.NAME_ONLY:
            n = len(a.pitfalls) + len(a.warnings)
            if n:
                lines.append(f"WARN {n} warning{'s' if n != 1 else ''} not shown")
            return "\n".join(lines)
        r = RANK[level]
        lines += [f"PITFALL {WARN} {sev}: {text}" for sev, text in a.pitfalls]
        lines += [f"WARN {WARN} {w}" for w in a.warnings]
        lines += [t for rk, t in a.keyed if r <= rk]
        cols, hidden = shown_columns(eff, a, level)
        if cols:
            lines.append("COLUMNS " + " | ".join(f"{v.name} {v.column.native_type}" for v in cols))
        for v in cols:
            lines += [t for rk, t in v.keyed if r <= rk]
        if hidden:
            lines.append(f"MORE {hidden} columns")
        return "\n".join(lines)


class RouterProfile:
    # one line per table, no join paths: the view packs the tables into one block
    compact = True
    show_paths = False

    def render_table(self, eff: TableKnowledge, level: DetailLevel) -> str:
        return finish(eff, self._render(eff, level))

    def _render(self, eff: TableKnowledge, level: DetailLevel) -> str:
        a = analyse(eff)
        head = a.title
        if a.never_query is not None:
            return f"{head} — {WARN} NEVER QUERY: {a.never_query}"
        if level is DetailLevel.NAME_ONLY or not a.description:
            return head
        return f"{head} — {a.description}"


_PROFILES: dict[str, Profile] = {
    "agent": AgentProfile(),
    "planner": PlannerProfile(),
    "verifier": VerifierProfile(),
    "router": RouterProfile(),
}


def register_profile(name: str, profile: Profile, *, replace: bool = False) -> None:
    """Add a profile (spec 21). Replacing an existing name needs ``replace=True``."""
    if name in _PROFILES and not replace:
        raise ValueError(f"profile already registered: {name}")
    _PROFILES[name] = profile


def _plugin(name: str, obj: object) -> None:
    profile = plugins.instance(obj)
    if not callable(getattr(profile, "render_table", None)):
        raise TypeError("a profile plugin needs render_table(knowledge, level)")
    register_profile(name, profile)


def get_profile(name: str) -> Profile:
    plugins.discover("profiles", _plugin)  # entry points ``schemaguru.profiles`` (spec 21)
    try:
        return _PROFILES[name]
    except KeyError:
        raise KeyError(f"unknown profile: {name} (known: {', '.join(sorted(_PROFILES))})") from None


def profile_names() -> list[str]:
    return sorted(_PROFILES)


__all__ = [
    "AgentProfile",
    "PlannerProfile",
    "Profile",
    "RouterProfile",
    "VerifierProfile",
    "get_profile",
    "ident",
    "profile_names",
    "register_profile",
]
