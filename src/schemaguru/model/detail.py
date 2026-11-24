"""Detail levels a table block can be rendered at (spec 12.1 step 4).

Shared by scoping (which picks a level per table) and rendering (which draws it); both live in
layer 6 and may not import each other, so the enum lives here.
"""

from __future__ import annotations

from enum import Enum


class DetailLevel(Enum):
    """Richest first; iteration order is the degradation order."""

    FULL = "full"
    STANDARD = "standard"
    LITE = "lite"
    NAME_ONLY = "name-only"
