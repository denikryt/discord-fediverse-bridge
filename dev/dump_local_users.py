#!/usr/bin/env python3
"""Run the local user-identity backup CLI from a source checkout.

This wrapper exists for operators who expect a project-root script under
`dev/`. The implementation stays in `src.user_identity_dump` so the behavior is
shared with the packaged entry point and can be tested without shelling out.
"""

from __future__ import annotations

import sys
from pathlib import Path

# When this script is invoked directly from a source checkout, Python places the
# `dev/` directory on sys.path. Add the project root so `src` imports resolve the
# same way they do for the packaged CLI entry point.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.user_identity_dump import main


if __name__ == "__main__":
    raise SystemExit(main())
