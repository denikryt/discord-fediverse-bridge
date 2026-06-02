"""Tests for Lemmyverse community parsing, ranking, and cache behavior."""

from __future__ import annotations

import asyncio
import gzip
import json
from dataclasses import dataclass

import httpx
import pytest

from src.lemmyverse_communities import (
    DISCORD_CHOICE_VALUE_LIMIT,
    LEMMYVERSE_CACHE_TTL_SECONDS,
    LemmyverseCommunityCache,
    autocomplete_lemmyverse_communities,
    parse_lemmyverse_communities,
)


def _row(
    name: str,
    host: str,
    *,
    title: str | None = None,
    actor_id: str | None = None,
    deleted: bool = False,
    removed: bool = False,
) -> dict[str, object]:
    """Build one Lemmy community-view row for parser tests."""
    return {
        "community": {
            "name": name,
            "title": title or name.title(),
            "actor_id": actor_id or f"https://{host}/c/{name}",
            "deleted": deleted,
            "removed": removed,
        }
    }


class _FakeClock:
    """Mutable monotonic clock used to test TTL refresh behavior."""

    def __init__(self) -> None:
        """Start the fake clock at zero seconds."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the current fake monotonic time."""
        return self.now


@dataclass
class _FakeResponse:
    """Minimal httpx-like response carrying raw feed bytes."""

    content: bytes

    def raise_for_status(self) -> None:
        """Match the success response API used by the cache."""
        return None


class _FakeClient:
    """Async context manager fake for Lemmyverse feed downloads."""

    def __init__(self, owner: "_FakeClientFactory") -> None:
        """Capture the factory so calls and payloads can be coordinated."""
        self._owner = owner

    async def __aenter__(self) -> "_FakeClient":
        """Return the fake client for async-with usage."""
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """No cleanup is required for the in-memory fake client."""
        return None

    async def get(self, url: str, headers: dict[str, str]) -> _FakeResponse:
        """Return the next configured payload or raise the configured error."""
        self._owner.calls += 1
        if self._owner.error is not None:
            raise self._owner.error
        return _FakeResponse(self._owner.payloads[-1])


class _FakeClientFactory:
    """Factory matching the cache's http_client_factory contract."""

    def __init__(self, *payloads: bytes) -> None:
        """Store payloads and expose call counters for assertions."""
        self.payloads = list(payloads)
        self.calls = 0
        self.error: Exception | None = None

    def __call__(self) -> _FakeClient:
        """Build one fake async HTTP client."""
        return _FakeClient(self)


def _payload(*rows: dict[str, object]) -> bytes:
    """Encode rows as the JSON bytes accepted by the parser/cache."""
    return json.dumps(list(rows)).encode("utf-8")


def test_parse_lemmyverse_communities_filters_and_derives_fields() -> None:
    """Parser keeps valid nested communities and skips unselectable rows."""
    too_long = "https://example.com/c/" + "a" * (DISCORD_CHOICE_VALUE_LIMIT + 1)
    entries = parse_lemmyverse_communities(
        [
            _row("technology", "lemmy.world", title="Technology"),
            _row("deleted", "lemmy.world", deleted=True),
            _row("removed", "lemmy.world", removed=True),
            {"community": {"name": "missing"}},
            _row("bad", "lemmy.world", actor_id="not-a-url"),
            _row("long", "example.com", actor_id=too_long),
        ]
    )

    assert len(entries) == 1
    assert entries[0].name == "technology"
    assert entries[0].title == "Technology"
    assert entries[0].actor_id == "https://lemmy.world/c/technology"
    assert entries[0].host == "lemmy.world"
    assert entries[0].handle == "!technology@lemmy.world"


def test_parse_lemmyverse_communities_supports_gzip_and_exact_limit() -> None:
    """Gzip bytes parse correctly and actor_id length 100 remains selectable."""
    actor_id = "https://lemmy.world/c/" + "a" * (100 - len("https://lemmy.world/c/"))
    raw = _payload(_row("longname", "lemmy.world", actor_id=actor_id))

    entries = parse_lemmyverse_communities(gzip.compress(raw))

    assert len(entries) == 1
    assert entries[0].actor_id == actor_id
    assert len(entries[0].actor_id) == 100


@pytest.mark.asyncio
async def test_autocomplete_ranking_limit_and_allowlist() -> None:
    """Global autocomplete ranks exact matches and filters disallowed hosts."""
    factory = _FakeClientFactory(
        _payload(
            _row("other", "blocked.example", title="Technology Other"),
            _row("tech", "lemmy.world", title="Tech"),
            _row("technology", "lemmy.world", title="Technology"),
            _row("mytechnology", "lemmy.world", title="My Technology"),
        )
    )
    cache = LemmyverseCommunityCache(http_client_factory=factory)

    choices = await autocomplete_lemmyverse_communities(
        cache,
        current="technology@lemmy.world",
        allowlist=["lemmy.world"],
    )

    assert choices[0] == ("Technology (technology@lemmy.world)", "https://lemmy.world/c/technology")
    assert all("blocked.example" not in choice[1] for choice in choices)


@pytest.mark.asyncio
async def test_autocomplete_empty_query_preserves_feed_order_and_caps_choices() -> None:
    """Empty global query returns deterministic feed order with Discord's cap."""
    rows = [_row(f"community{i}", "lemmy.world") for i in range(30)]
    cache = LemmyverseCommunityCache(http_client_factory=_FakeClientFactory(_payload(*rows)))

    choices = await autocomplete_lemmyverse_communities(cache, current="", allowlist=[])

    assert len(choices) == 25
    assert choices[0][1] == "https://lemmy.world/c/community0"
    assert choices[-1][1] == "https://lemmy.world/c/community24"


@pytest.mark.asyncio
async def test_cache_ttl_stale_failure_and_cold_failure() -> None:
    """Cache refreshes after one hour and degrades to stale/empty on errors."""
    clock = _FakeClock()
    factory = _FakeClientFactory(_payload(_row("one", "lemmy.world")))
    cache = LemmyverseCommunityCache(http_client_factory=factory, monotonic=clock)

    assert LEMMYVERSE_CACHE_TTL_SECONDS == 3600
    assert [entry.name for entry in await cache.get_entries()] == ["one"]
    assert [entry.name for entry in await cache.get_entries()] == ["one"]
    assert factory.calls == 1

    clock.now = 3601
    factory.error = RuntimeError("network down")
    assert [entry.name for entry in await cache.get_entries()] == ["one"]
    assert factory.calls == 2

    cold_factory = _FakeClientFactory(_payload(_row("unused", "lemmy.world")))
    cold_factory.error = RuntimeError("network down")
    cold_cache = LemmyverseCommunityCache(http_client_factory=cold_factory)
    assert await cold_cache.get_entries() == []


@pytest.mark.asyncio
async def test_cache_coalesces_concurrent_refreshes() -> None:
    """Concurrent cold autocomplete requests share one feed download."""
    factory = _FakeClientFactory(_payload(_row("one", "lemmy.world")))
    cache = LemmyverseCommunityCache(http_client_factory=factory)

    results = await asyncio.gather(cache.get_entries(), cache.get_entries(), cache.get_entries())

    assert [[entry.name for entry in result] for result in results] == [["one"], ["one"], ["one"]]
    assert factory.calls == 1
