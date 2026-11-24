"""PII detection, masking and outbound sweep (layer 3, spec 11).

Detection is by value shape first, then column-name signals, then cardinality, never by name
alone. Everything here is pure: no I/O besides reading the bundled name list once.
"""

from __future__ import annotations

from schemaguru.pii.detectors import (
    Detector,
    PIIFinding,
    all_detectors,
    classify_column,
    register_detector,
    reset_detectors,
)
from schemaguru.pii.mask import mask_free_text, mask_text, mask_value, scrub_tree, sql_template
from schemaguru.pii.policy import ValuePolicy
from schemaguru.pii.sweep import sweep

__all__ = [
    "Detector",
    "PIIFinding",
    "ValuePolicy",
    "all_detectors",
    "classify_column",
    "mask_free_text",
    "mask_text",
    "mask_value",
    "register_detector",
    "reset_detectors",
    "scrub_tree",
    "sql_template",
    "sweep",
]
