"""``python -m schemaguru`` runs the command line."""

from __future__ import annotations

import sys

from schemaguru.cli import main

if __name__ == "__main__":
    sys.exit(main())
