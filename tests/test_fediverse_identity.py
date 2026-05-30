"""Behavior tests for remote Fediverse identity normalization helpers."""

from __future__ import annotations

import pytest

from src.fediverse_identity import (
    InvalidRemoteActorHandle,
    extract_remote_actor_handle_from_actor_url,
    normalize_remote_actor_handle,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("alice@example.com", "alice@example.com"),
        (" Alice@Example.COM ", "Alice@example.com"),
    ],
)
def test_normalize_remote_actor_handle_accepts_displayed_handle_shape(raw: str, expected: str) -> None:
    """Moderators can paste the handle shape shown in Discord command output."""
    assert normalize_remote_actor_handle(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "@alice@example.com",
        "acct:alice@example.com",
        "https://example.com/u/alice",
        "http://example.com/u/alice",
        "alice",
        "alice@",
        "@example.com",
        "alice@example.com@extra",
    ],
)
def test_normalize_remote_actor_handle_rejects_non_v1_inputs(raw: str) -> None:
    """The v1 ban command intentionally accepts handles, not AP URLs or acct URIs."""
    with pytest.raises(InvalidRemoteActorHandle):
        normalize_remote_actor_handle(raw)


@pytest.mark.parametrize(
    ("actor_url", "expected"),
    [
        ("https://example.com/u/alice", "alice@example.com"),
        ("https://example.com/users/alice", "alice@example.com"),
        ("https://Example.COM/users/Alice/", "Alice@example.com"),
    ],
)
def test_extract_remote_actor_handle_from_actor_url_uses_common_actor_paths(actor_url: str, expected: str) -> None:
    """Inbound hot-path matching may derive a best-effort handle without network calls."""
    assert extract_remote_actor_handle_from_actor_url(actor_url) == expected


@pytest.mark.parametrize("actor_url", ["invalid-url", "https://example.com/", "https:///users/alice"])
def test_extract_remote_actor_handle_from_actor_url_returns_none_for_unknown_shapes(actor_url: str) -> None:
    """Unparseable actor URLs are not treated as global actor bans."""
    assert extract_remote_actor_handle_from_actor_url(actor_url) is None
