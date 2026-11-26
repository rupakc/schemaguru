"""The text of a lineage walk (0.2.0 spec 4.8): every line declared or parsed, never measured,
and never a join path."""

from __future__ import annotations

from schemaguru.knowledge.lineage import LineageResult


def lineage_text(result: LineageResult) -> str:
    """One line per edge, ``downstream <- upstream``, sources in brackets."""
    if not result.edges:
        return f"No lineage recorded for {result.root} ({result.direction})."
    outside = {n.ref for n in result.nodes if not n.in_catalog}
    lines = [
        f"Lineage {result.direction} of {result.root} ({result.level} level, depth "
        f"{result.depth}) — declared/parsed, unverified; not a join path"
    ]
    for e in result.edges:
        note = " (not in catalog)" if e.upstream in outside else ""
        how = f" ({e.transform})" if e.transform else ""
        lines.append(f"  {e.downstream} <- {e.upstream}{note}{how} [{', '.join(e.sources)}]")
    if result.truncated:
        lines.append(f"(truncated at {len(result.nodes)} nodes)")
    lines += [f"cycle: {' -> '.join(c)}" for c in result.cycles]
    return "\n".join(lines)


__all__ = ["lineage_text"]
