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
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import urlparse

import httpx

from .bridge_policy import BridgePolicySnapshot, EffectivePolicyEntry, PolicyType, normalize_instance_subject

logger = logging.getLogger(__name__)

LEMMYVERSE_COMMUNITY_FEED_URL = "https://data.lemmyverse.net/data/community.full.json"
LEMMYVERSE_CACHE_TTL_SECONDS = 3600
LEMMYVERSE_RETRY_DELAYS_SECONDS = (1.0, 2.0, 3.0)
LEMMYVERSE_CACHE_FILE = Path(".cache/lemmyverse/community.full.json")
DISCORD_CHOICE_VALUE_LIMIT = 100
DISCORD_CHOICE_NAME_LIMIT = 100
DISCORD_AUTOCOMPLETE_LIMIT = 25


@dataclass(frozen=True)
class LemmyverseCommunityEntry:
    """Compact searchable representation of one Lemmyverse community row.

    The entry stores only fields required for autocomplete ranking and submit
    resolution. It intentionally excludes the original feed row so the in-memory
    index remains smaller than the downloaded JSON cache file.
    """

    name: str
    title: str
    actor_id: str
    host: str
    handle: str
    active_users_month: int | None
    search_text: str
    feed_order: int


class LemmyverseCommunityCache:
    """Refresh and serve a disk-backed Lemmyverse community index.

    The downloaded feed is persisted as plain JSON on disk, not gzip and not as
    the authoritative cache in RAM. Autocomplete serves a compact parsed index
    from memory after loading that JSON file, while the file mtime decides when
    lazy refresh should contact Lemmyverse again.
    """

    def __init__(
        self,
        *,
        feed_url: str = LEMMYVERSE_COMMUNITY_FEED_URL,
        cache_path: str | Path = LEMMYVERSE_CACHE_FILE,
        ttl_seconds: int = LEMMYVERSE_CACHE_TTL_SECONDS,
        retry_delays_seconds: Iterable[float] = LEMMYVERSE_RETRY_DELAYS_SECONDS,
        http_client_factory: Callable[[], AbstractAsyncContextManager[httpx.AsyncClient]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        """Create one process-local cache with injectable filesystem/network edges."""
        self.feed_url = feed_url
        self.cache_path = Path(cache_path)
        self.ttl_seconds = ttl_seconds
        self.retry_delays_seconds = tuple(retry_delays_seconds)
        self._http_client_factory = http_client_factory or self._default_http_client_factory
        self._monotonic = monotonic
        self._wall_time = wall_time
        self._sleep = sleep
        self._entries: list[LemmyverseCommunityEntry] = []
        self._loaded_mtime: float | None = None
        self._refresh_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None

    async def get_entries(self) -> list[LemmyverseCommunityEntry]:
        """Return cached entries, refreshing synchronously when no usable file exists.

        This method is intended for tests and explicit warm-up paths. Discord
        autocomplete uses ``get_entries_for_autocomplete`` so cold network work
        never blocks an interaction response.
        """
        if self._ensure_memory_index_loaded():
            if self._is_disk_cache_fresh():
                return list(self._entries)
            if self._entries:
                await self._refresh_if_needed()
                return list(self._entries)

        await self._refresh_if_needed()
        self._ensure_memory_index_loaded()
        return list(self._entries)

    async def get_entries_for_autocomplete(self) -> list[LemmyverseCommunityEntry]:
        """Return the current index immediately and refresh stale data later.

        The method first tries to load the JSON cache file from disk. When the
        file is missing or expired it starts one background refresh and returns
        immediately, preserving Discord's autocomplete response deadline.
        """
        loaded_from_disk = self._ensure_memory_index_loaded()
        if not loaded_from_disk or not self._is_disk_cache_fresh():
            self._ensure_background_refresh()
        return list(self._entries)

    async def _refresh_if_needed(self) -> None:
        """Refresh stale cache data while coalescing concurrent refresh attempts."""
        async with self._refresh_lock:
            self._ensure_memory_index_loaded()
            if self._is_disk_cache_fresh():
                return
            await self._refresh_from_network_with_retry()

    async def _refresh_from_network_with_retry(self) -> None:
        """Download the feed, retrying briefly when no cache is available."""
        delays = self.retry_delays_seconds if not self._entries else ()
        max_attempts = len(delays) + 1
        started_at = self._monotonic()
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(
                    "Starting Lemmyverse community cache refresh attempt %s/%s from %s",
                    attempt,
                    max_attempts,
                    self.feed_url,
                )
                entries = await self._fetch_entries_to_disk()
                self._entries = entries
                self._loaded_mtime = self._cache_file_mtime()
                logger.info(
                    "Finished Lemmyverse community cache refresh: entries=%s cache_path=%s duration=%.3fs",
                    len(entries),
                    self.cache_path,
                    self._monotonic() - started_at,
                )
                return
            except Exception:
                logger.exception(
                    "Failed Lemmyverse community cache refresh attempt %s/%s",
                    attempt,
                    max_attempts,
                )
                if attempt > len(delays):
                    logger.warning(
                        "Giving up Lemmyverse community cache refresh: entries_available=%s cache_path=%s",
                        bool(self._entries),
                        self.cache_path,
                    )
                    return
                delay = delays[attempt - 1]
                logger.info("Retrying Lemmyverse community cache refresh in %.1fs", delay)
                await self._sleep(delay)

    def _ensure_background_refresh(self) -> None:
        """Start one non-blocking refresh task when no usable task is running."""
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        logger.info("Scheduling Lemmyverse community cache background refresh")
        self._refresh_task = loop.create_task(self._refresh_if_needed())

    def _ensure_memory_index_loaded(self) -> bool:
        """Load the compact in-memory index from the JSON cache file when needed."""
        mtime = self._cache_file_mtime()
        if mtime is None:
            return False
        if self._entries and self._loaded_mtime == mtime:
            return True
        try:
            payload = self.cache_path.read_text(encoding="utf-8")
            self._entries = parse_lemmyverse_communities(payload)
            self._loaded_mtime = mtime
            logger.info(
                "Loaded Lemmyverse community cache from disk: entries=%s cache_path=%s",
                len(self._entries),
                self.cache_path,
            )
            return True
        except Exception:
            logger.exception("Failed to load Lemmyverse community cache file %s", self.cache_path)
            self._entries = []
            self._loaded_mtime = None
            return False

    def _is_disk_cache_fresh(self) -> bool:
        """Return whether the on-disk JSON cache file is within the TTL window."""
        mtime = self._cache_file_mtime()
        return mtime is not None and (self._wall_time() - mtime) < self.ttl_seconds

    def _cache_file_mtime(self) -> float | None:
        """Return cache-file modification time, or ``None`` when it is absent."""
        try:
            return self.cache_path.stat().st_mtime
        except FileNotFoundError:
            return None

    async def _fetch_entries_to_disk(self) -> list[LemmyverseCommunityEntry]:
        """Download the feed, save plain JSON to disk, then parse that file."""
        async with self._http_client_factory() as client:
            response = await client.get(self.feed_url, headers={"Accept": "application/json"})
            response.raise_for_status()
            payload = _load_feed_payload(response.content)
        self._write_json_cache_file(payload)
        return parse_lemmyverse_communities(self.cache_path.read_text(encoding="utf-8"))

    def _write_json_cache_file(self, payload: Any) -> None:
        """Persist the feed as a plain JSON file with an atomic replace."""
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.cache_path.with_name(f"{self.cache_path.name}.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(self.cache_path)

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
    policy_snapshot: BridgePolicySnapshot | None = None,
    allowlist: list[str] | None = None,
) -> list[tuple[str, str]]:
    """Return ``(choice_name, actor_url)`` pairs for global autocomplete.

    ``allowlist`` remains a compatibility input for older callers. New runtime
    paths pass one effective snapshot so dynamic blocklist precedence applies.
    """
    if policy_snapshot is None:
        entries = tuple(
            EffectivePolicyEntry(
                PolicyType.FEDERATION_ALLOW,
                normalize_instance_subject(subject),
                "bootstrap",
            )
            for subject in (allowlist or [])
        )
        policy_snapshot = BridgePolicySnapshot(entries)
    if hasattr(cache, "get_entries_for_autocomplete"):
        entries = await cache.get_entries_for_autocomplete()
    else:
        entries = await cache.get_entries()
    allowed_entries = [
        entry for entry in entries
        if policy_snapshot.federation_decision(entry.actor_id).allowed
    ]
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
        for key in ("community_details", "communities", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        for value in payload.values():
            if isinstance(value, list):
                return value
    return []


def _parse_entry(item: Any, *, feed_order: int) -> LemmyverseCommunityEntry | None:
    """Parse one Lemmyverse row into an autocomplete entry."""
    if not isinstance(item, dict):
        return None
    community = item.get("community")
    if isinstance(community, dict):
        counts = item.get("counts") if isinstance(item.get("counts"), dict) else None
        return _parse_lemmy_api_entry(community, counts=counts, feed_order=feed_order)
    return _parse_lemmyverse_flat_entry(item, feed_order=feed_order)


def _parse_lemmy_api_entry(
    community: dict[str, Any],
    *,
    counts: dict[str, Any] | None = None,
    feed_order: int,
) -> LemmyverseCommunityEntry | None:
    """Parse a Lemmy ``CommunityView`` nested ``community`` object."""
    if community.get("deleted") is True or community.get("removed") is True:
        return None

    actor_id = str(community.get("actor_id") or "").strip()
    name = str(community.get("name") or "").strip()
    title = str(community.get("title") or name).strip() or name
    active_users_month = _parse_optional_int((counts or {}).get("users_active_month"))
    return _build_entry(
        actor_id=actor_id,
        name=name,
        title=title,
        active_users_month=active_users_month,
        feed_order=feed_order,
    )


def _parse_lemmyverse_flat_entry(item: dict[str, Any], *, feed_order: int) -> LemmyverseCommunityEntry | None:
    """Parse the public Lemmyverse ``community.full.json`` flat row shape."""
    if item.get("isSuspicious") is True:
        return None
    actor_id = str(item.get("url") or "").strip()
    name = str(item.get("name") or "").strip()
    title = str(item.get("title") or name).strip() or name
    counts = item.get("counts") if isinstance(item.get("counts"), dict) else {}
    active_users_month = _parse_optional_int(counts.get("users_active_month"))
    return _build_entry(
        actor_id=actor_id,
        name=name,
        title=title,
        active_users_month=active_users_month,
        feed_order=feed_order,
    )


def _build_entry(
    *,
    actor_id: str,
    name: str,
    title: str,
    active_users_month: int | None = None,
    feed_order: int,
) -> LemmyverseCommunityEntry | None:
    """Build one normalized autocomplete entry after row-shape extraction."""
    if not actor_id or len(actor_id) > DISCORD_CHOICE_VALUE_LIMIT:
        return None
    parsed = urlparse(actor_id)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None

    if not name:
        return None
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
        active_users_month=active_users_month,
        search_text=search_text,
        feed_order=feed_order,
    )


def _rank_entries(entries: list[LemmyverseCommunityEntry], *, query: str) -> list[LemmyverseCommunityEntry]:
    """Return matching entries with the most active communities first.

    Lemmyverse autocomplete is primarily a discovery surface, so monthly-active
    users is the main ranking signal both before and after textual filtering.
    Match buckets are retained only as a deterministic tie-breaker for equally
    active communities.
    """
    normalized = query.strip().lower()
    if not normalized:
        return sorted(entries, key=_activity_rank_key)

    scored: list[tuple[tuple[int, int, int], LemmyverseCommunityEntry]] = []
    for entry in entries:
        score = _match_score(entry, normalized)
        if score is not None:
            scored.append(((_activity_sort_value(entry.active_users_month), score, entry.feed_order), entry))
    return [entry for _, entry in sorted(scored, key=lambda item: item[0])]


def _activity_rank_key(entry: LemmyverseCommunityEntry) -> tuple[int, int]:
    """Sort by monthly-active users descending while preserving stable ties."""
    return (_activity_sort_value(entry.active_users_month), entry.feed_order)


def _activity_sort_value(value: int | None) -> int:
    """Return the ascending sort value for a descending activity ranking."""
    return -(value or 0)


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


def _parse_optional_int(value: Any) -> int | None:
    """Return one non-negative integer from feed counters when available."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _format_active_month(value: int | None) -> str | None:
    """Format monthly-active users compactly for Discord choice names."""
    if value is None:
        return None
    return f"{value:,}".replace(",", " ") + " active/mo"


def _choice_name(entry: LemmyverseCommunityEntry) -> str:
    """Build a Discord-safe display label while preserving handle and activity context."""
    activity = _format_active_month(entry.active_users_month)
    suffix = f" ({entry.name}@{entry.host}"
    if activity is not None:
        suffix += f" · {activity}"
    suffix += ")"
    title_budget = DISCORD_CHOICE_NAME_LIMIT - len(suffix)
    if title_budget <= 0:
        return suffix.strip()[:DISCORD_CHOICE_NAME_LIMIT]
    title = entry.title
    if len(title) > title_budget:
        # Reserve one ellipsis character so labels stay visibly truncated while
        # keeping the disambiguating host/name suffix intact whenever possible.
        title = f"{title[: max(title_budget - 1, 0)]}…"
    return f"{title}{suffix}"
