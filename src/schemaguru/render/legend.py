"""The legend block (spec 13.3): present only when a view contains a warning marker."""

from __future__ import annotations

WARN = "⚠"
LEGEND_SENTENCE = "Absence of a warning is not verification."

LEGEND = "\n".join(
    [
        "Legend:",
        f"  {WARN} a hazard: measured, declared by a person, or drafted (then marked (draft)).",
        "  ≈ / ≥ estimated from a sample of a large table; never verified.",
        "  (draft) written by a language model and not checked.",
        "  measured <date> an observed number; verified <date> a claim that passed its check.",
        "  (human, unchecked) / (declared, unchecked) stated by a person or a declaration;"
        " not yet checked against the data.",
        f"  {LEGEND_SENTENCE}",
    ]
)


def needs_legend(texts: list[str]) -> bool:
    """Whether any of ``texts`` carries the warning marker."""
    return any(WARN in t for t in texts)
