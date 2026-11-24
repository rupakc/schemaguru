"""The refresh report: JSON and Markdown (spec 15.6)."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from schemaguru._util.hashing import canonical_json
from schemaguru.catalog.crawl import CrawlReport
from schemaguru.knowledge.diff import Change, subject_key
from schemaguru.measure.scheduler import MeasureReport
from schemaguru.model.facts import Fact
from schemaguru.refresh.classify import ChangeClass

EXIT_OK, EXIT_REVIEW, EXIT_NOTHING_MEASURED = 0, 1, 3
_ORDER = (
    ChangeClass.REVIEW,
    ChangeClass.NOT_MEASURED,
    ChangeClass.PRE_EXISTING,
    ChangeClass.BENIGN,
)
_TITLES = {
    ChangeClass.REVIEW: "Needs review",
    ChangeClass.NOT_MEASURED: "Not measured (unknown, not fixed)",
    ChangeClass.PRE_EXISTING: "Drift (pre-existing contradictions)",
    ChangeClass.BENIGN: "Benign",
}


def _fact_json(f: Fact | None) -> dict[str, Any] | None:
    if f is None:
        return None
    return {
        "id": f.id,
        "status": f.status.value,
        "value": json.loads(canonical_json(f.value)),
    }


@dataclass
class RefreshReport:
    changes: list[tuple[Change, ChangeClass]]
    crawl: CrawlReport
    measure: MeasureReport
    exit_code: int
    snapshot_id: str | None = None
    previous_snapshot_id: str | None = None
    checks_run: frozenset[str] = field(default_factory=frozenset)
    missing_checks: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        c = Counter(cls.value for _, cls in self.changes)
        return {k.value: c.get(k.value, 0) for k in _ORDER}

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "snapshot_id": self.snapshot_id,
            "previous_snapshot_id": self.previous_snapshot_id,
            "checks_run": sorted(self.checks_run),
            "missing_checks": list(self.missing_checks),
            "counts": self.counts(),
            "changes": [
                {
                    "class": cls.value,
                    "kind": c.kind.name,
                    "subject": subject_key(c.subject),
                    "change": c.change,
                    "before": _fact_json(c.before),
                    "after": _fact_json(c.after),
                }
                for c, cls in self.changes
            ],
            "crawl": {
                "tables_seen": self.crawl.tables_seen,
                "tables_written": self.crawl.tables_written,
                "withdrawn": self.crawl.withdrawn,
                "errors": [d.to_dict() for d in self.crawl.errors],
            },
            "measure": {
                "measured_tables": [t.canonical() for t in self.measure.measured_tables],
                "facts_written": self.measure.facts_written,
                "over_budget": [t.canonical() for t in self.measure.over_budget],
                "sampled_tables": [t.canonical() for t in self.measure.sampled_tables],
                "bytes": self.measure.bytes,
                "errors": [d.to_dict() for d in self.measure.errors],
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def to_markdown(self) -> str:
        n = self.counts()
        lines = [
            "# Refresh report",
            "",
            f"- exit code: {self.exit_code}",
            f"- tables seen {self.crawl.tables_seen}, written {self.crawl.tables_written}, "
            f"withdrawn {self.crawl.withdrawn}",
            f"- measured {len(self.measure.measured_tables)} table(s), "
            f"{self.measure.facts_written} fact(s), {self.measure.bytes} bytes",
            "- changes: " + ", ".join(f"{k.value.lower()} {n[k.value]}" for k in _ORDER),
        ]
        if self.missing_checks:
            lines.append(
                "- missing checks (run before, not now): " + ", ".join(self.missing_checks)
            )
        for cls in _ORDER:
            rows = [(c, k) for c, k in self.changes if k is cls]
            if not rows:
                continue
            lines += ["", f"## {_TITLES[cls]} ({len(rows)})", ""]
            for c, _ in rows:
                lines.append(f"- {c.kind.name} {subject_key(c.subject)}: {c.change}")
        if self.exit_code == EXIT_NOTHING_MEASURED:
            lines += ["", "Nothing was measured; no verdict can be drawn from this run."]
        return "\n".join(lines) + "\n"
