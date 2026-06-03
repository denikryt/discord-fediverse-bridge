"""Tests for Lemmyverse community parsing, ranking, and cache behavior."""

from __future__ import annotations

import asyncio
import gzip
import json
import os
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


def test_parse_lemmyverse_communities_supports_lemmyverse_summary_wrapper() -> None:
    """Parser reads the public Lemmyverse full feed's community_details wrapper."""
    entries = parse_lemmyverse_communities(
        {
            "crawled_communities": 1,
            "subscribers": 10,
            "community_details": [_row("technology", "lemmy.world", title="Technology")],
        }
    )

    assert len(entries) == 1
    assert entries[0].actor_id == "https://lemmy.world/c/technology"


def test_parse_lemmyverse_communities_supports_public_flat_feed_rows() -> None:
    """Parser reads the current data.lemmyverse.net community.full.json list rows."""
    entries = parse_lemmyverse_communities(
        [
            {
                "baseurl": "lemmy.world",
                "url": "https://lemmy.world/c/planetoftheapes",
                "name": "planetoftheapes",
                "title": "Planet of the Apes",
                "counts": {"users_active_month": 12},
                "isSuspicious": False,
            },
            {
                "baseurl": "bad.example",
                "url": "https://bad.example/c/suspicious",
                "name": "suspicious",
                "title": "Suspicious",
                "isSuspicious": True,
            },
        ]
    )

    assert len(entries) == 1
    assert entries[0].name == "planetoftheapes"
    assert entries[0].title == "Planet of the Apes"
    assert entries[0].actor_id == "https://lemmy.world/c/planetoftheapes"
    assert entries[0].host == "lemmy.world"
    assert entries[0].handle == "!planetoftheapes@lemmy.world"
    assert entries[0].active_users_month == 12


def test_parse_lemmyverse_communities_supports_gzip_and_exact_limit() -> None:
    """Gzip bytes parse correctly and actor_id length 100 remains selectable."""
    actor_id = "https://lemmy.world/c/" + "a" * (100 - len("https://lemmy.world/c/"))
    raw = _payload(_row("longname", "lemmy.world", actor_id=actor_id))

    entries = parse_lemmyverse_communities(gzip.compress(raw))

    assert len(entries) == 1
    assert entries[0].actor_id == actor_id
    assert len(entries[0].actor_id) == 100


@pytest.mark.asyncio
async def test_autocomplete_ranking_limit_and_allowlist(tmp_path) -> None:
    """Global autocomplete ranks exact matches and filters disallowed hosts."""
    factory = _FakeClientFactory(
        _payload(
            _row("other", "blocked.example", title="Technology Other"),
            _row("tech", "lemmy.world", title="Tech"),
            _row("technology", "lemmy.world", title="Technology"),
            _row("mytechnology", "lemmy.world", title="My Technology"),
        )
    )
    cache = LemmyverseCommunityCache(http_client_factory=factory, cache_path=tmp_path / "lemmyverse.json")

    await cache.get_entries()

    choices = await autocomplete_lemmyverse_communities(
        cache,
        current="technology@lemmy.world",
        allowlist=["lemmy.world"],
    )

    assert choices[0] == ("Technology (technology@lemmy.world)", "https://lemmy.world/c/technology")
    assert all("blocked.example" not in choice[1] for choice in choices)


@pytest.mark.asyncio
async def test_autocomplete_empty_query_preserves_feed_order_and_caps_choices(tmp_path) -> None:
    """Empty global query returns deterministic feed order with Discord's cap."""
    rows = [_row(f"community{i}", "lemmy.world") for i in range(30)]
    cache = LemmyverseCommunityCache(
        http_client_factory=_FakeClientFactory(_payload(*rows)),
        cache_path=tmp_path / "lemmyverse.json",
    )

    await cache.get_entries()

    choices = await autocomplete_lemmyverse_communities(cache, current="", allowlist=[])

    assert len(choices) == 25
    assert choices[0][1] == "https://lemmy.world/c/community0"
    assert choices[-1][1] == "https://lemmy.world/c/community24"


@pytest.mark.asyncio
async def test_cache_ttl_stale_failure_and_cold_failure(tmp_path) -> None:
    """Cache refreshes after one hour and degrades to stale/empty on errors."""
    clock = _FakeClock()
    factory = _FakeClientFactory(_payload(_row("one", "lemmy.world")))
    cache = LemmyverseCommunityCache(
        http_client_factory=factory,
        monotonic=clock,
        wall_time=lambda: clock.now,
        cache_path=tmp_path / "warm.json",
    )

    assert LEMMYVERSE_CACHE_TTL_SECONDS == 3600
    assert [entry.name for entry in await cache.get_entries()] == ["one"]
    assert [entry.name for entry in await cache.get_entries()] == ["one"]
    assert factory.calls == 1

    (tmp_path / "warm.json").touch()
    os.utime(tmp_path / "warm.json", (0, 0))
    clock.now = 3601
    factory.error = RuntimeError("network down")
    assert [entry.name for entry in await cache.get_entries()] == ["one"]
    assert factory.calls == 2

    cold_factory = _FakeClientFactory(_payload(_row("unused", "lemmy.world")))
    cold_factory.error = RuntimeError("network down")
    cold_cache = LemmyverseCommunityCache(
        http_client_factory=cold_factory,
        cache_path=tmp_path / "cold.json",
        retry_delays_seconds=(),
    )
    assert await cold_cache.get_entries() == []
    assert await cold_cache.get_entries() == []
    assert cold_factory.calls == 2


@pytest.mark.asyncio
async def test_cache_coalesces_concurrent_refreshes(tmp_path) -> None:
    """Concurrent cold autocomplete requests share one feed download."""
    factory = _FakeClientFactory(_payload(_row("one", "lemmy.world")))
    cache = LemmyverseCommunityCache(http_client_factory=factory, cache_path=tmp_path / "lemmyverse.json")

    results = await asyncio.gather(cache.get_entries(), cache.get_entries(), cache.get_entries())

    assert [[entry.name for entry in result] for result in results] == [["one"], ["one"], ["one"]]
    assert factory.calls == 1


class _BlockingClient:
    """Async fake that blocks feed download until the test releases it."""

    def __init__(self, owner: "_BlockingClientFactory") -> None:
        """Store the shared synchronization owner for the fake request."""
        self._owner = owner

    async def __aenter__(self) -> "_BlockingClient":
        """Return the fake client for async-with usage."""
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """No cleanup is required for the blocking fake."""
        return None

    async def get(self, url: str, headers: dict[str, str]) -> _FakeResponse:
        """Block the HTTP response so autocomplete responsiveness is observable."""
        self._owner.calls += 1
        self._owner.started.set()
        await self._owner.release.wait()
        return _FakeResponse(self._owner.payload)


class _BlockingClientFactory:
    """Factory that coordinates one delayed Lemmyverse feed download."""

    def __init__(self, payload: bytes) -> None:
        """Create per-test events after the event loop is running."""
        self.payload = payload
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def __call__(self) -> _BlockingClient:
        """Build one blocking async HTTP client."""
        return _BlockingClient(self)


@pytest.mark.asyncio
async def test_autocomplete_does_not_wait_for_cold_cache_refresh(tmp_path) -> None:
    """Cold global autocomplete returns quickly and refreshes for later calls."""
    factory = _BlockingClientFactory(_payload(_row("one", "lemmy.world")))
    cache = LemmyverseCommunityCache(http_client_factory=factory, cache_path=tmp_path / "lemmyverse.json")

    choices = await autocomplete_lemmyverse_communities(cache, current="one", allowlist=[])

    assert choices == []
    await asyncio.wait_for(factory.started.wait(), timeout=1)
    assert factory.calls == 1

    # A second autocomplete while refresh is still in flight must not start a
    # second full-feed download. It remains empty until the background task has
    # parsed the first response.
    choices = await autocomplete_lemmyverse_communities(cache, current="one", allowlist=[])
    assert choices == []
    assert factory.calls == 1

    factory.release.set()
    assert cache._refresh_task is not None
    await asyncio.wait_for(cache._refresh_task, timeout=1)

    choices = await autocomplete_lemmyverse_communities(cache, current="one", allowlist=[])
    assert choices == [("One (one@lemmy.world)", "https://lemmy.world/c/one")]


class _RetryClientFactory:
    """Factory that fails a configured number of cold-refresh attempts."""

    def __init__(self, payload: bytes, *, failures: int) -> None:
        """Store the final payload and the number of errors to raise first."""
        self.payload = payload
        self.failures = failures
        self.calls = 0

    def __call__(self) -> "_RetryClient":
        """Build one retry-aware fake client."""
        return _RetryClient(self)


class _RetryClient:
    """Async fake client that errors before returning a final payload."""

    def __init__(self, owner: _RetryClientFactory) -> None:
        """Capture shared retry state for one request."""
        self._owner = owner

    async def __aenter__(self) -> "_RetryClient":
        """Return the fake client for async-with usage."""
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """No cleanup is required for the retry fake."""
        return None

    async def get(self, url: str, headers: dict[str, str]) -> _FakeResponse:
        """Raise until the configured failures are consumed, then succeed."""
        self._owner.calls += 1
        if self._owner.calls <= self._owner.failures:
            raise RuntimeError("temporary network failure")
        return _FakeResponse(self._owner.payload)


class _SleepRecorder:
    """Async sleep fake that records retry delays without slowing tests."""

    def __init__(self) -> None:
        """Start with no recorded retry delays."""
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        """Record the requested delay and return immediately."""
        self.delays.append(delay)


@pytest.mark.asyncio
async def test_cold_cache_retries_one_two_three_then_writes_plain_json(tmp_path) -> None:
    """Cold empty cache retries 1/2/3 seconds and persists decoded JSON."""
    cache_path = tmp_path / "community.full.json"
    sleeper = _SleepRecorder()
    factory = _RetryClientFactory(gzip.compress(_payload(_row("one", "lemmy.world"))), failures=3)
    cache = LemmyverseCommunityCache(
        http_client_factory=factory,
        cache_path=cache_path,
        retry_delays_seconds=(1, 2, 3),
        sleep=sleeper,
    )

    entries = await cache.get_entries()

    assert [entry.name for entry in entries] == ["one"]
    assert factory.calls == 4
    assert sleeper.delays == [1, 2, 3]
    assert cache_path.read_bytes().startswith(b"[")
    assert not cache_path.read_bytes().startswith(b"\x1f\x8b")
    assert json.loads(cache_path.read_text(encoding="utf-8"))[0]["community"]["name"] == "one"


@pytest.mark.asyncio
async def test_fresh_disk_cache_loads_without_network(tmp_path) -> None:
    """A fresh JSON file is the lazy cache source and avoids HTTP fetches."""
    cache_path = tmp_path / "community.full.json"
    cache_path.write_text(json.dumps([_row("disk", "lemmy.world")]), encoding="utf-8")
    factory = _FakeClientFactory(_payload(_row("network", "lemmy.world")))
    cache = LemmyverseCommunityCache(http_client_factory=factory, cache_path=cache_path)

    choices = await autocomplete_lemmyverse_communities(cache, current="disk", allowlist=[])

    assert choices == [("Disk (disk@lemmy.world)", "https://lemmy.world/c/disk")]
    assert factory.calls == 0
