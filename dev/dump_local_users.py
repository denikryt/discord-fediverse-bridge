#!/usr/bin/env python3
"""Compatibility wrapper for the packaged local-user identity dump CLI."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.user_identity_dump import main


if __name__ == "__main__":
    raise SystemExit(main())
