"""Tests for the shared project release version contract."""

from __future__ import annotations

import json
from pathlib import Path
import tomllib

import pytest

from src.project_version import APP_VERSION, _read_project_version

ROOT = Path(__file__).resolve().parent.parent


def test_project_version_matches_package_metadata() -> None:
    """Bridge and gateway package metadata must mirror the canonical VERSION."""
    canonical = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package = json.loads((ROOT / "fedify-gateway/package.json").read_text(encoding="utf-8"))

    assert canonical == "0.1.0"
    assert APP_VERSION == canonical
    assert pyproject["project"]["version"] == canonical
    assert package["version"] == canonical


def test_project_version_rejects_missing_file(tmp_path: Path) -> None:
    """A missing canonical version must fail clearly instead of using a fallback."""
    with pytest.raises(RuntimeError, match="version file is missing"):
        _read_project_version(tmp_path / "VERSION")


def test_project_version_rejects_empty_file(tmp_path: Path) -> None:
    """An empty canonical version must fail clearly instead of hiding bad packaging."""
    version_file = tmp_path / "VERSION"
    version_file.write_text("\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="version file is empty"):
        _read_project_version(version_file)
