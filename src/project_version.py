"""Load the shared project release version for the Python bridge.

The root VERSION file is the canonical release identifier used by the bridge,
gateway, dashboard, and future container images. It is read once at import time
so request handlers do not touch the filesystem.
"""

from __future__ import annotations

from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def _read_project_version(path: Path = VERSION_FILE) -> str:
    """Read and validate one non-empty project version from ``path``."""
    try:
        version = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(f"Project version file is missing: {path}") from exc

    if not version:
        raise RuntimeError(f"Project version file is empty: {path}")
    return version


APP_VERSION = _read_project_version()
