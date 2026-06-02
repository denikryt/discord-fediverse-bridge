"""Unified community discovery for remote Lemmy and bridge-owned communities.

This module owns the pre-operation resolution logic behind `/subscribe-channel`.
It keeps Discord command adapters thin by centralizing:

- instance-origin normalization;
- direct actor URL and fediverse-handle parsing;
- bridge-specific discovery endpoint fetching;
- Lemmy API fallback resolution;
- typed source selection for local bridge, remote bridge, and remote Lemmy.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

import httpx

if TYPE_CHECKING:
    from .config import Settings
    from .lemmy_client import LemmyClient

logger = logging.getLogger(__name__)

BRIDGE_DISCOVERY_PATH = "/.well-known/discord-fediverse-bridge/communities"


class CommunityResolutionError(RuntimeError):
    """Carry one moderator-facing resolution error.

    Command adapters use this error to return one precise ephemeral message
    instead of silently guessing the wrong remote-community source type.
    """


class BridgeDiscoveryUnavailable(RuntimeError):
    """Signal that a remote host does not expose the bridge discovery endpoint."""


@dataclass(frozen=True)
class ParsedCommunityHandle:
    """Describe one normalized `!name@host` or `name@host` reference."""

    name: str
    host: str
    handle: str


@dataclass(frozen=True)
class ParsedCommunityUrl:
    """Describe one parsed community actor URL or Lemmy-style alias URL."""

    origin: str
    slug: str
    actor_url: str
    path_kind: Literal["community", "lemmy_alias", "other"]


@dataclass(frozen=True)
class BridgeCommunitySummary:
    """Public discovery data for one bridge-owned local community."""

    id: int
    slug: str
    name: str
    title: str
    description: str | None
    actor_id: str
    alternate_actor_id: str
    handle: str


@dataclass(frozen=True)
class ResolvedCommunity:
    """Describe one community selected by `/subscribe-channel` before dispatch."""

    source: Literal["remote_lemmy", "remote_bridge", "local_bridge"]
    actor_id: str
    name: str | None
    numeric_id: int | None
    handle: str
    local_community_id: int | None = None
    remote_software: str | None = None


def normalize_instance_domain(value: str) -> str:
    """Normalize a moderator-entered instance/domain value into one origin URL.

    The command accepts bare hosts and URLs with paths. Discovery comparisons
    and allowlist checks need one stable origin string instead of the raw input.
    """

    candidate = value.strip()
    if not candidate:
        raise CommunityResolutionError("Instance domain is required.")
    if not candidate.startswith(("http://", "https://")):
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    if not parsed.scheme or not parsed.hostname:
        raise CommunityResolutionError(
            "Could not parse the instance domain. Use a hostname like `lemmy.world` or a full https:// URL."
        )
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def parse_community_handle(value: str) -> ParsedCommunityHandle | None:
    """Parse one community handle, accepting both `!name@host` and `name@host`."""

    raw = value.strip()
    if "@" not in raw:
        return None
    candidate = raw[1:] if raw.startswith("!") else raw
    name, host = candidate.partition("@")[::2]
    if not name or not host or "@" in host or "/" in host:
        raise CommunityResolutionError(
            "Malformed community handle. Use `!name@example.com` or `name@example.com`."
        )
    normalized_host = host.strip().lower()
    if not normalized_host:
        raise CommunityResolutionError(
            "Malformed community handle. Use `!name@example.com` or `name@example.com`."
        )
    return ParsedCommunityHandle(
        name=name.strip(),
        host=normalized_host,
        handle=f"!{name.strip()}@{normalized_host}",
    )


def parse_actor_url(value: str) -> ParsedCommunityUrl | None:
    """Parse one direct actor URL or Lemmy-style `/c/<slug>` community alias."""

    raw = value.strip()
    if not raw.startswith(("http://", "https://")):
        return None
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.hostname:
        raise CommunityResolutionError("Malformed community URL.")
    origin = normalize_instance_domain(raw)
    path_parts = [part for part in parsed.path.split("/") if part]
    slug = path_parts[-1] if path_parts else ""
    if len(path_parts) == 2 and path_parts[0] == "communities" and slug:
        path_kind: Literal["community", "lemmy_alias", "other"] = "community"
    elif len(path_parts) == 2 and path_parts[0] == "c" and slug:
        path_kind = "lemmy_alias"
    else:
        path_kind = "other"
    return ParsedCommunityUrl(origin=origin, slug=slug, actor_url=raw, path_kind=path_kind)


def infer_reference_origin(value: str) -> str | None:
    """Infer the remote origin from a direct actor URL or community handle."""

    parsed_url = parse_actor_url(value)
    if parsed_url is not None:
        return parsed_url.origin
    parsed_handle = parse_community_handle(value)
    if parsed_handle is not None:
        return normalize_instance_domain(parsed_handle.host)
    return None


def is_bridge_origin(origin: str, settings: Settings | None) -> bool:
    """Return whether *origin* refers to this bridge deployment."""

    if settings is None:
        return False
    normalized = normalize_instance_domain(origin)
    local_origins = {
        getattr(settings, "normalized_public_bridge_base_url", "").rstrip("/"),
        getattr(settings, "normalized_fedify_origin", "").rstrip("/"),
    }
    return normalized in {candidate for candidate in local_origins if candidate}


async def fetch_bridge_community_summaries(origin: str) -> list[BridgeCommunitySummary]:
    """Fetch the public bridge-owned community list from one bridge origin."""

    discovery_url = f"{normalize_instance_domain(origin)}{BRIDGE_DISCOVERY_PATH}"
    timeout = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.get(discovery_url, headers={"Accept": "application/json"})
        except httpx.HTTPError as error:
            raise CommunityResolutionError(
                f"Could not reach the bridge discovery endpoint at {normalize_instance_domain(origin)}."
            ) from error
    if response.status_code == 404:
        raise BridgeDiscoveryUnavailable(discovery_url)
    if response.status_code >= 400:
        raise CommunityResolutionError(
            f"Bridge discovery failed for {normalize_instance_domain(origin)} with HTTP {response.status_code}."
        )
    payload = response.json()
    if payload.get("software") != "discord-fediverse-bridge":
        raise BridgeDiscoveryUnavailable(discovery_url)
    communities = payload.get("communities")
    if not isinstance(communities, list):
        raise CommunityResolutionError("Bridge discovery returned an invalid community list payload.")

    summaries: list[BridgeCommunitySummary] = []
    for item in communities:
        if not isinstance(item, dict):
            continue
        summaries.append(
            BridgeCommunitySummary(
                id=int(item["id"]),
                slug=str(item["slug"]),
                name=str(item.get("name") or item["slug"]),
                title=str(item.get("title") or item.get("name") or item["slug"]),
                description=str(item["description"]) if item.get("description") is not None else None,
                actor_id=str(item["actor_id"]),
                alternate_actor_id=str(item["alternate_actor_id"]),
                handle=str(item["handle"]),
            )
        )
    return summaries


async def autocomplete_communities(
    settings: Settings | None,
    *,
    instance_domain: str,
    current: str,
    fetch_bridge_communities: Any,
    lemmy_client_cls: type[LemmyClient],
) -> list[tuple[str, str]]:
    """Return `(choice_name, choice_value)` pairs for community autocomplete."""

    origin = normalize_instance_domain(instance_domain)
    query = current.lower().strip()
    if is_bridge_origin(origin, settings):
        try:
            summaries = await fetch_bridge_communities(origin)
        except Exception:
            logger.exception("Failed same-instance bridge discovery for %s", origin)
            return []
        return _build_bridge_autocomplete_choices(summaries, query=query, source="local_bridge")

    try:
        summaries = await fetch_bridge_communities(origin)
    except BridgeDiscoveryUnavailable:
        summaries = None
    except Exception:
        logger.exception("Failed remote bridge discovery for %s", origin)
        summaries = None

    if summaries is not None:
        return _build_bridge_autocomplete_choices(summaries, query=query, source="remote_bridge")

    remote_client = lemmy_client_cls(origin)
    try:
        communities = await remote_client.list_communities(limit=50, type_="Local")
    except Exception:
        logger.exception("Failed to fetch communities from %s for autocomplete", origin)
        return []
    finally:
        await remote_client.close()

    choices: list[tuple[str, str]] = []
    for item in communities:
        community = item.get("community", {})
        name = str(community.get("name", ""))
        title = str(community.get("title", "") or name)
        actor_id = str(community.get("actor_id", ""))
        numeric_id = community.get("id")
        if query and query not in name.lower() and query not in title.lower():
            continue
        choices.append((f"{title} ({name})", f"lemmy:{actor_id}|{name}|{numeric_id or ''}"))
        if len(choices) >= 25:
            break
    return choices


async def resolve_selected_community(
    settings: Settings | None,
    *,
    instance_domain: str | None,
    community_value: str,
    fetch_bridge_communities: Any,
    lemmy_client_cls: type[LemmyClient],
) -> ResolvedCommunity:
    """Resolve one moderator-entered community value into a typed target.

    Encoded autocomplete payloads, direct actor URLs, and fediverse handles carry
    their own origin. Plain names remain instance-scoped and therefore require
    ``instance_domain`` so global Lemmyverse mode cannot guess between duplicate
    community names on different hosts.
    """

    encoded = _parse_encoded_community_value(community_value)
    if encoded is not None:
        return encoded

    raw_instance = (instance_domain or "").strip()
    instance_origin = normalize_instance_domain(raw_instance) if raw_instance else None
    parsed_url = parse_actor_url(community_value)
    parsed_handle = parse_community_handle(community_value)
    local_origin = is_bridge_origin(instance_origin, settings) if instance_origin is not None else False

    if parsed_url is not None:
        if is_bridge_origin(parsed_url.origin, settings):
            return await _resolve_from_bridge_summaries(
                parsed_url.origin,
                fetch_bridge_communities=fetch_bridge_communities,
                source="local_bridge",
                actor_url=parsed_url.actor_url,
                slug=parsed_url.slug,
                handle=None,
                strict=True,
            )
        bridge_result = await _try_resolve_remote_bridge(
            parsed_url.origin,
            fetch_bridge_communities=fetch_bridge_communities,
            actor_url=parsed_url.actor_url,
            slug=parsed_url.slug,
            handle=None,
        )
        if bridge_result is not None:
            return bridge_result
        return await _resolve_remote_lemmy(
            parsed_url.origin,
            slug_or_name=parsed_url.slug,
            lemmy_client_cls=lemmy_client_cls,
            actor_id_hint=parsed_url.actor_url,
        )

    if parsed_handle is not None:
        handle_origin = normalize_instance_domain(parsed_handle.host)
        if is_bridge_origin(handle_origin, settings):
            return await _resolve_from_bridge_summaries(
                handle_origin,
                fetch_bridge_communities=fetch_bridge_communities,
                source="local_bridge",
                actor_url=None,
                slug=parsed_handle.name,
                handle=parsed_handle.handle,
                strict=True,
            )
        bridge_result = await _try_resolve_remote_bridge(
            handle_origin,
            fetch_bridge_communities=fetch_bridge_communities,
            actor_url=None,
            slug=parsed_handle.name,
            handle=parsed_handle.handle,
        )
        if bridge_result is not None:
            return bridge_result
        return await _resolve_remote_lemmy(
            handle_origin,
            slug_or_name=parsed_handle.name,
            lemmy_client_cls=lemmy_client_cls,
        )

    if instance_origin is None:
        raise CommunityResolutionError(
            "Select a community from autocomplete, paste a full community URL, use !name@instance, or provide instance_domain."
        )

    if local_origin:
        return await _resolve_from_bridge_summaries(
            instance_origin,
            fetch_bridge_communities=fetch_bridge_communities,
            source="local_bridge",
            actor_url=None,
            slug=community_value.strip(),
            handle=None,
            strict=True,
        )

    bridge_result = await _try_resolve_remote_bridge(
        instance_origin,
        fetch_bridge_communities=fetch_bridge_communities,
        actor_url=None,
        slug=community_value.strip(),
        handle=None,
    )
    if bridge_result is not None:
        return bridge_result
    return await _resolve_remote_lemmy(
        instance_origin,
        slug_or_name=community_value.strip(),
        lemmy_client_cls=lemmy_client_cls,
    )


def _build_bridge_autocomplete_choices(
    summaries: list[BridgeCommunitySummary],
    *,
    query: str,
    source: Literal["local_bridge", "remote_bridge"],
) -> list[tuple[str, str]]:
    """Encode bridge discovery summaries into Discord autocomplete values."""

    choices: list[tuple[str, str]] = []
    prefix = "bridge-local" if source == "local_bridge" else "bridge-remote"
    for summary in summaries:
        haystacks = (summary.slug.lower(), summary.name.lower(), summary.title.lower())
        if query and not any(query in candidate for candidate in haystacks):
            continue
        hidden_id = str(summary.id) if source == "local_bridge" else ""
        choices.append((f"{summary.title} ({summary.name})", f"{prefix}:{summary.actor_id}|{summary.name}|{hidden_id}"))
        if len(choices) >= 25:
            break
    return choices


def _parse_encoded_community_value(value: str) -> ResolvedCommunity | None:
    """Decode one autocomplete payload into a typed community result."""

    if "|" in value and not value.startswith(("lemmy:", "bridge-remote:", "bridge-local:")):
        # Legacy autocomplete payloads omitted the source prefix and always
        # meant "remote Lemmy". Keeping this compatibility path avoids
        # breaking existing command tests and stale Discord command caches.
        actor_id, name, id_or_empty = _split_payload(value)
        hostname = urlparse(actor_id).hostname
        handle = f"!{name}@{hostname}" if name and hostname else actor_id
        return ResolvedCommunity(
            source="remote_lemmy",
            actor_id=actor_id,
            name=name or None,
            numeric_id=int(id_or_empty) if id_or_empty else None,
            handle=handle,
            remote_software="lemmy",
        )
    if ":" not in value:
        return None
    prefix, raw_payload = value.split(":", 1)
    if prefix not in {"lemmy", "bridge-remote", "bridge-local"}:
        return None
    actor_id, name, id_or_empty = _split_payload(raw_payload)
    hostname = urlparse(actor_id).hostname
    handle = f"!{name}@{hostname}" if name and hostname else actor_id
    if prefix == "lemmy":
        return ResolvedCommunity(
            source="remote_lemmy",
            actor_id=actor_id,
            name=name or None,
            numeric_id=int(id_or_empty) if id_or_empty else None,
            handle=handle,
            remote_software="lemmy",
        )
    if prefix == "bridge-remote":
        return ResolvedCommunity(
            source="remote_bridge",
            actor_id=actor_id,
            name=name or None,
            numeric_id=None,
            handle=handle,
            remote_software="discord-fediverse-bridge",
        )
    return ResolvedCommunity(
        source="local_bridge",
        actor_id=actor_id,
        name=name or None,
        numeric_id=None,
        handle=handle,
        local_community_id=int(id_or_empty) if id_or_empty else None,
        remote_software="discord-fediverse-bridge",
    )


def _split_payload(payload: str) -> tuple[str, str, str]:
    """Split one `actor|name|id` payload without losing empty trailing fields."""

    parts = payload.split("|", 2)
    actor_id = parts[0] if len(parts) > 0 else payload
    name = parts[1] if len(parts) > 1 else ""
    id_or_empty = parts[2] if len(parts) > 2 else ""
    return actor_id, name, id_or_empty


async def _try_resolve_remote_bridge(
    origin: str,
    *,
    fetch_bridge_communities: Any,
    actor_url: str | None,
    slug: str,
    handle: str | None,
) -> ResolvedCommunity | None:
    """Resolve one remote host through bridge discovery when the endpoint exists."""

    try:
        return await _resolve_from_bridge_summaries(
            origin,
            fetch_bridge_communities=fetch_bridge_communities,
            source="remote_bridge",
            actor_url=actor_url,
            slug=slug,
            handle=handle,
            strict=False,
        )
    except (BridgeDiscoveryUnavailable, CommunityResolutionError):
        return None


async def _resolve_from_bridge_summaries(
    origin: str,
    *,
    fetch_bridge_communities: Any,
    source: Literal["local_bridge", "remote_bridge"],
    actor_url: str | None,
    slug: str,
    handle: str | None,
    strict: bool,
) -> ResolvedCommunity:
    """Resolve one bridge community from the bridge discovery endpoint."""

    try:
        summaries = await fetch_bridge_communities(origin)
    except BridgeDiscoveryUnavailable:
        raise
    except CommunityResolutionError:
        raise
    except Exception as error:
        raise CommunityResolutionError(
            f"Could not load bridge community discovery from {normalize_instance_domain(origin)}."
        ) from error

    match = _match_bridge_summary(summaries, actor_url=actor_url, slug=slug, handle=handle)
    if match is None:
        if strict:
            raise CommunityResolutionError("That bridge-owned community could not be found on the selected instance.")
        raise BridgeDiscoveryUnavailable(origin)

    return ResolvedCommunity(
        source=source,
        actor_id=match.actor_id,
        name=match.name,
        numeric_id=None,
        handle=match.handle,
        local_community_id=match.id if source == "local_bridge" else None,
        remote_software="discord-fediverse-bridge",
    )


def _match_bridge_summary(
    summaries: list[BridgeCommunitySummary],
    *,
    actor_url: str | None,
    slug: str,
    handle: str | None,
) -> BridgeCommunitySummary | None:
    """Find one bridge summary by actor URL, handle, or slug/name."""

    slug_lower = slug.lower().strip()
    handle_lower = handle.lower().strip() if handle is not None else None
    actor_url_normalized = actor_url.rstrip("/") if actor_url is not None else None
    for summary in summaries:
        if actor_url_normalized is not None and actor_url_normalized in {
            summary.actor_id.rstrip("/"),
            summary.alternate_actor_id.rstrip("/"),
        }:
            return summary
        if handle_lower is not None and summary.handle.lower() == handle_lower:
            return summary
        if slug_lower in {
            summary.slug.lower(),
            summary.name.lower(),
            summary.title.lower(),
        }:
            return summary
    return None


async def _resolve_remote_lemmy(
    origin: str,
    *,
    slug_or_name: str,
    lemmy_client_cls: type[LemmyClient],
    actor_id_hint: str | None = None,
) -> ResolvedCommunity:
    """Resolve one remote Lemmy community through the existing Lemmy API."""

    remote_client = lemmy_client_cls(origin)
    try:
        community = await remote_client.resolve_community(name=slug_or_name)
    except Exception as error:
        raise CommunityResolutionError(
            "Could not resolve that remote community through the Lemmy API."
        ) from error
    finally:
        await remote_client.close()

    actor_id = str(community.get("actor_id") or actor_id_hint or "")
    if not actor_id:
        raise CommunityResolutionError("The remote Lemmy API did not return a community actor URL.")
    community_name = str(community.get("name") or slug_or_name)
    hostname = urlparse(actor_id).hostname
    handle = f"!{community_name}@{hostname}" if hostname else actor_id
    numeric_id = community.get("id")
    return ResolvedCommunity(
        source="remote_lemmy",
        actor_id=actor_id,
        name=community_name,
        numeric_id=int(numeric_id) if numeric_id is not None else None,
        handle=handle,
        remote_software="lemmy",
    )
