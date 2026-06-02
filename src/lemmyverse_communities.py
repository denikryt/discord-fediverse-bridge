"""Lemmyverse-backed global community autocomplete cache.

This module owns best-effort discovery from the external Lemmyverse community
feed. The feed is not ActivityPub protocol state and is not used as durable
application state; it is only a cached UX index for Discord autocomplete.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import httpx

from .federation_policy import is_instance_allowed

logger = logging.getLogger(__name__)

LEMMYVERSE_COMMUNITY_FEED_URL = "https://data.lemmyverse.net/data/community.full.json"
LEMMYVERSE_CACHE_TTL_SECONDS = 3600
DISCORD_CHOICE_VALUE_LIMIT = 100
DISCORD_CHOICE_NAME_LIMIT = 100
DISCORD_AUTOCOMPLETE_LIMIT = 25


@dataclass(frozen=True)
class LemmyverseCommunityEntry:
    """Compact searchable representation of one Lemmyverse community row.

    The entry stores only fields required for autocomplete ranking and submit
    resolution. It intentionally excludes the original feed row so the cache
    remains small and does not become a broad external-data dump.
    """

    name: str
    title: str
    actor_id: str
    host: str
    handle: str
    search_text: str
    feed_order: int


class LemmyverseCommunityCache:
    """Refresh and serve a process-local Lemmyverse community index.

    The cache coalesces concurrent refreshes so Discord autocomplete bursts do
    not download the full feed multiple times. Refresh failures degrade to a
    stale cache when one exists, or an empty cache on cold startup.
    """

    def __init__(
        self,
        *,
        feed_url: str = LEMMYVERSE_COMMUNITY_FEED_URL,
        ttl_seconds: int = LEMMYVERSE_CACHE_TTL_SECONDS,
        http_client_factory: Callable[[], AbstractAsyncContextManager[httpx.AsyncClient]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create one process-local cache with injectable network/time edges."""
        self.feed_url = feed_url
        self.ttl_seconds = ttl_seconds
        self._http_client_factory = http_client_factory or self._default_http_client_factory
        self._monotonic = monotonic
        self._entries: list[LemmyverseCommunityEntry] = []
        self._last_refresh: float | None = None
        self._refresh_lock = asyncio.Lock()

    async def get_entries(self) -> list[LemmyverseCommunityEntry]:
        """Return cached entries, refreshing once when the TTL has expired."""
        if self._is_fresh():
            return list(self._entries)

        # Autocomplete calls can arrive concurrently while a user types. The
        # lock guarantees one download/parse per cold or expired cache window.
        async with self._refresh_lock:
            if self._is_fresh():
                return list(self._entries)
            try:
                self._entries = await self._fetch_entries()
                self._last_refresh = self._monotonic()
            except Exception:
                logger.exception("Failed to refresh Lemmyverse community autocomplete cache")
                # Stale entries are better than breaking autocomplete. A cold
                # cache returns [] because there is no safe discovery data yet.
                if self._last_refresh is None:
                    self._entries = []
            return list(self._entries)

    def _is_fresh(self) -> bool:
        """Return whether the cache can be used without another refresh."""
        return self._last_refresh is not None and (self._monotonic() - self._last_refresh) < self.ttl_seconds

    async def _fetch_entries(self) -> list[LemmyverseCommunityEntry]:
        """Download and parse the current Lemmyverse community feed."""
        async with self._http_client_factory() as client:
            response = await client.get(self.feed_url, headers={"Accept": "application/json"})
            response.raise_for_status()
            return parse_lemmyverse_communities(response.content)

    @staticmethod
    def _default_http_client_factory() -> httpx.AsyncClient:
        """Build the default HTTP client used by production autocomplete."""
        timeout = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)
        return httpx.AsyncClient(timeout=timeout)


def parse_lemmyverse_communities(raw: bytes | str | Any) -> list[LemmyverseCommunityEntry]:
    """Parse a Lemmyverse feed payload into compact autocomplete entries.

    The parser accepts raw plain JSON bytes, gzip-compressed JSON bytes, an
    already decoded JSON string, or a decoded Python object. Rows without a
    usable nested ``community.actor_id`` are skipped rather than making the
    entire autocomplete cache fail.
    """
    payload = _load_feed_payload(raw)
    entries: list[LemmyverseCommunityEntry] = []
    for feed_order, item in enumerate(_iter_feed_rows(payload)):
        entry = _parse_entry(item, feed_order=feed_order)
        if entry is not None:
            entries.append(entry)
    return entries


async def autocomplete_lemmyverse_communities(
    cache: LemmyverseCommunityCache,
    *,
    current: str,
    allowlist: Iterable[str],
) -> list[tuple[str, str]]:
    """Return ``(choice_name, actor_url)`` pairs for global autocomplete."""
    entries = await cache.get_entries()
    allowed_entries = [entry for entry in entries if is_instance_allowed(entry.actor_id, allowlist)]
    ranked_entries = _rank_entries(allowed_entries, query=current)
    return [(_choice_name(entry), entry.actor_id) for entry in ranked_entries[:DISCORD_AUTOCOMPLETE_LIMIT]]


def _load_feed_payload(raw: bytes | str | Any) -> Any:
    """Decode the feed from compressed/plain JSON or return decoded objects."""
    if isinstance(raw, bytes):
        # The public endpoint is JSON today, but tests and mirrors may provide
        # gzip bytes. Checking the gzip magic number avoids guessing from URLs.
        if raw.startswith(b"\x1f\x8b"):
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _iter_feed_rows(payload: Any) -> list[Any]:
    """Return the most likely community-view list from tolerant feed shapes."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("communities", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        for value in payload.values():
            if isinstance(value, list):
                return value
    return []


def _parse_entry(item: Any, *, feed_order: int) -> LemmyverseCommunityEntry | None:
    """Parse one nested Lemmy community-view row into an autocomplete entry."""
    if not isinstance(item, dict):
        return None
    community = item.get("community")
    if not isinstance(community, dict):
        return None
    if community.get("deleted") is True or community.get("removed") is True:
        return None

    actor_id = str(community.get("actor_id") or "").strip()
    if not actor_id or len(actor_id) > DISCORD_CHOICE_VALUE_LIMIT:
        return None
    parsed = urlparse(actor_id)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None

    name = str(community.get("name") or "").strip()
    if not name:
        return None
    title = str(community.get("title") or name).strip() or name
    host = parsed.hostname.lower()
    handle = f"!{name}@{host}"
    search_text = "\n".join(
        [
            name.lower(),
            f"{name}@{host}".lower(),
            handle.lower(),
            title.lower(),
            actor_id.lower(),
            host,
        ]
    )
    return LemmyverseCommunityEntry(
        name=name,
        title=title,
        actor_id=actor_id,
        host=host,
        handle=handle,
        search_text=search_text,
        feed_order=feed_order,
    )


def _rank_entries(entries: list[LemmyverseCommunityEntry], *, query: str) -> list[LemmyverseCommunityEntry]:
    """Rank matching entries deterministically for Discord autocomplete."""
    normalized = query.lower().strip()
    if not normalized:
        return sorted(entries, key=lambda entry: entry.feed_order)

    scored: list[tuple[tuple[int, int], LemmyverseCommunityEntry]] = []
    for entry in entries:
        score = _match_score(entry, normalized)
        if score is not None:
            scored.append(((score, entry.feed_order), entry))
    return [entry for _, entry in sorted(scored, key=lambda item: item[0])]


def _match_score(entry: LemmyverseCommunityEntry, query: str) -> int | None:
    """Return the rank bucket for one query match, or ``None`` for no match."""
    name = entry.name.lower()
    host = entry.host.lower()
    handle_without_bang = f"{name}@{host}"
    handle_with_bang = f"!{handle_without_bang}"
    title = entry.title.lower()
    actor_id = entry.actor_id.lower()
    if query in {handle_without_bang, handle_with_bang}:
        return 0
    if query == name:
        return 1
    if name.startswith(query):
        return 2
    if handle_without_bang.startswith(query) or handle_with_bang.startswith(query):
        return 3
    if title.startswith(query):
        return 4
    if any(query in candidate for candidate in (name, handle_without_bang, handle_with_bang, title, host, actor_id)):
        return 5
    return None


def _choice_name(entry: LemmyverseCommunityEntry) -> str:
    """Build a Discord-safe display label while preserving handle context."""
    suffix = f" ({entry.name}@{entry.host})"
    title_budget = DISCORD_CHOICE_NAME_LIMIT - len(suffix)
    if title_budget <= 0:
        return suffix.strip()[:DISCORD_CHOICE_NAME_LIMIT]
    title = entry.title
    if len(title) > title_budget:
        # Reserve one ellipsis character so labels stay visibly truncated while
        # keeping the disambiguating host/name suffix intact whenever possible.
        title = f"{title[: max(title_budget - 1, 0)]}…"
    return f"{title}{suffix}"
