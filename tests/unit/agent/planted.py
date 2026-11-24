"""Personal data planted in every place a value or free text can reach an agent tool.

Each key is a secret; its value says where it was planted. A privacy test sends every tool,
``context()`` and ``prompt_block`` over a store holding these facts and asserts no secret
leaves in ``masked`` or ``none`` mode (spec 11, 18).
"""

from __future__ import annotations

from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind as F
from schemaguru.overlays.schema import GLOBAL_SUBJECT
from tests.golden.shop_knowledge import (
    MEASURED_AT,
    NOW,
    SAMPLE,
    c,
    drafted,
    human,
    measured,
    shop_facts,
    t,
)

SECRETS: dict[str, str] = {
    "a1@example.com": "PII column, partial value",
    "Zebulon Quartzfeld": "PII column, the text around a partial value",
    "b2@example.com": "human declared VALUES on a PII column",
    "c3@example.com": "live-rows predicate literal (customers)",
    "d4@example.com": "pitfall rule",
    "e5@example.com": "column description",
    "f6@example.com": "glossary definition",
    "g7@example.com": "never-query reason",
    "h8@example.com": "measured ERROR reason",
    "i9@example.com": "freshness max_value",
    "j10@example.com": "use-cases question",
    "k11@example.com": "verified live-rows literal (join snippet)",
    "l12@example.com": "observed extra vs declared on a non-PII column",
    "m13@example.com": "sampled observed value on a PII column",
    "n14@example.com": "unmeasurable reason",
    "o15@example.com": "human description on a column",
    "p16@example.com": "table description (search_tables)",
    "q17@example.com": "check_fact evidence (claim text, params, raw)",
}


def planted_facts() -> list[Fact]:
    base = [
        f for f in shop_facts() if not (f.kind.name == "SOFT_DELETE" and f.subject == t("payments"))
    ]
    extra = [
        measured(F.PII, c("customers", "phone"), {"category": "phone", "detector": "phone"}),
        measured(
            F.VALUES,
            c("customers", "phone"),
            {
                "case_sensitive": True,
                "split": None,
                "distinct": 2,
                "non_null": 2,
                "observed": [
                    {"value": "Zebulon Quartzfeld a1@example.com", "count": 3},
                    {"value": "Zebulon Quartzfeld", "count": 1},
                ],
            },
        ),
        human(
            F.VALUES,
            c("customers", "email"),
            {"values": ["b2@example.com"], "case_sensitive": True, "split": None},
        ),
        Fact.make(
            F.VALUES,
            c("customers", "email"),
            {
                "case_sensitive": True,
                "split": None,
                "observed": [{"value": "m13@example.com", "count": 1}],
                "fraction": "0.01",
                "approximate": True,
            },
            Provenance.MEASURED,
            Status.ESTIMATED,
            method=SAMPLE,
            source="measure",
            observed_at=MEASURED_AT,
        ),
        measured(F.PII, c("customers", "email"), {"category": "email", "detector": "email"}),
        human(
            F.SOFT_DELETE,
            t("customers"),
            {"predicate": "email <> 'c3@example.com'", "marker_columns": ["email"]},
        ),
        human(
            F.PITFALL,
            t("customers"),
            {"rule": "rows of d4@example.com are test data", "fix": None, "severity": "critical"},
        ),
        human(F.DESCRIPTION, c("customers", "segment"), {"text": "owner e5@example.com"}),
        human(F.NEVER_QUERY, t("accounts"), {"reason": "ask g7@example.com"}),
        Fact.make(
            F.ROW_COUNT,
            t("customers"),
            {"reason": "cast failed for 'h8@example.com'"},
            Provenance.MEASURED,
            Status.ERROR,
            method="exact",
            source="measure",
            observed_at=NOW,
        ),
        measured(
            F.FRESHNESS,
            t("customers"),
            {"column": "segment", "max_value": "zz i9@example.com", "age_seconds": 10},
        ),
        human(F.USE_CASES, t("customers"), {"questions": ["what did j10@example.com buy"]}),
        drafted(
            F.SOFT_DELETE,
            t("payments"),
            {"predicate": "note <> 'k11@example.com'", "marker_columns": []},
            Status.VERIFIED,
        ),
        human(
            F.VALUES,
            c("customers", "segment"),
            {"values": ["retail", "wholesale", "vip"], "case_sensitive": True, "split": None},
        ),
        measured(
            F.VALUES,
            c("customers", "segment"),
            {
                "case_sensitive": True,
                "split": None,
                "distinct": 4,
                "non_null": 4,
                "observed": [
                    {"value": "retail", "count": 1},
                    {"value": "Retail", "count": 1},
                    {"value": "note l12@example.com", "count": 2},
                    {"value": "NOTE L12@example.com", "count": 2},
                ],
            },
        ),
        Fact.make(
            F.NULL_RATE,
            c("customers", "phone"),
            {"reason": "unmeasurable because n14@example.com"},
            Provenance.MEASURED,
            Status.UNMEASURABLE,
            method="exact",
            source="measure",
            observed_at=MEASURED_AT,
        ),
        human(
            F.DESCRIPTION,
            c("customers", "email"),
            {"text": "primary contact, e.g. o15@example.com"},
        ),
        human(
            F.GLOSSARY,
            GLOBAL_SUBJECT,
            {
                "term": "vip",
                "definition": "customers like f6@example.com",
                "maps_to": [t("customers").to_dict()],
            },
        ),
        human(F.DESCRIPTION, t("orders"), {"text": "orders, owner p16@example.com"}),
    ]
    return base + extra


def leaks(text: str) -> list[str]:
    """The planted secrets present in ``text`` (case-insensitive; an address's local part
    followed by ``@`` counts too, so a half-masked address is caught)."""
    low = text.lower()
    return [s for s in SECRETS if s.lower() in low or s.split("@")[0].lower() + "@" in low]
