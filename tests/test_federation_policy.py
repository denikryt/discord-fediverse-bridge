"""Unit tests for the federation allowlist policy."""

from __future__ import annotations

import pytest

from src.federation_policy import is_instance_allowed


def test_allowlist_empty_permits_any_instance():
    # Empty allowlist means open federation — all instances are allowed.
    assert is_instance_allowed("https://lemmy.world/c/news", []) is True
    assert is_instance_allowed("https://beehaw.org/c/tech", []) is True


def test_allowlist_permits_listed_hostname():
    # A URL whose hostname appears in the allowlist must be permitted.
    assert is_instance_allowed("https://lemmy.world/c/news", ["lemmy.world"]) is True


def test_allowlist_rejects_unlisted_hostname():
    # A URL whose hostname is absent from the allowlist must be rejected.
    assert is_instance_allowed("https://beehaw.org/c/tech", ["lemmy.world"]) is False


def test_allowlist_handles_url_with_path():
    # Full community actor URLs (with path segments) must be parsed correctly.
    assert is_instance_allowed(
        "https://lemmy.world/c/technology", ["lemmy.world", "beehaw.org"]
    ) is True
    assert is_instance_allowed(
        "https://other.example/c/news", ["lemmy.world", "beehaw.org"]
    ) is False


def test_allowlist_check_is_case_insensitive():
    # Hostname comparison must be case-insensitive on both sides.
    assert is_instance_allowed("https://Lemmy.World/c/news", ["lemmy.world"]) is True
    assert is_instance_allowed("https://lemmy.world/c/news", ["Lemmy.World"]) is True


def test_allowlist_ignores_whitespace_in_entries():
    # Allowlist entries may have surrounding whitespace from env var parsing.
    assert is_instance_allowed("https://lemmy.world/c/news", [" lemmy.world "]) is True
