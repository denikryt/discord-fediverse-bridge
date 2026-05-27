"""Public dashboard aggregation and rendering for bridge instance metadata.

This module owns the public-data boundary for the human-facing dashboard shell
served from `/` and the JSON data endpoint served from `/dashboard/data`. It
explains instance state without exposing Discord-internal identifiers, secrets,
private keys, raw database paths, or internal service URLs.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DASHBOARD_HTML_PATH = WEB_DIR / "dashboard.html"


def build_dashboard_payload(runtime: Any) -> dict[str, object]:
    """Build the public dashboard payload from safe runtime state."""
    settings = runtime.settings
    origin = str(getattr(settings, "normalized_fedify_origin", settings.fedify_origin)).rstrip("/")
    origin_host = _hostname_from_url(origin) or ""
    actor_identifier = getattr(settings, "fedify_actor_identifier", "bridge")
    local_communities = runtime.database.local_communities.list_local_communities()
    registered_users = runtime.database.users.list_users()
    remote_subscribers = runtime.database.remote_subscribers.list_remote_subscribers_for_all(status="accepted")
    bridge_follows = runtime.database.bridge_actor_follows.list_bridge_actor_follows()

    remote_subscribers_by_community: dict[int, list[object]] = defaultdict(list)
    for remote_subscriber in remote_subscribers:
        remote_subscribers_by_community[getattr(remote_subscriber, "local_community_id")].append(remote_subscriber)

    community_payloads = []
    for community in sorted(
        local_communities,
        key=lambda row: (
            str(getattr(row, "display_name", "")).lower(),
            str(getattr(row, "slug", "")).lower(),
        ),
    ):
        community_remote_subscribers = remote_subscribers_by_community.get(getattr(community, "id"), [])
        remote_subscriber_payloads = []
        for remote_subscriber in sorted(
            community_remote_subscribers,
            key=lambda row: str(getattr(row, "remote_actor_id", "")).lower(),
        ):
            actor_url = getattr(remote_subscriber, "remote_actor_id")
            remote_subscriber_payloads.append(
                {
                    "actorUrl": actor_url,
                    "instanceHost": _hostname_from_url(actor_url)
                    or _hostname_from_url(getattr(remote_subscriber, "remote_inbox_url", "")),
                }
            )
        local_subscriber_count = runtime.database.local_subscribers.count_local_subscribers(getattr(community, "id"))
        community_payloads.append(
            {
                "slug": getattr(community, "slug"),
                "name": getattr(community, "display_name"),
                "description": getattr(community, "summary"),
                "relayHandle": _local_community_relay_handle(
                    getattr(community, "slug"),
                    origin_host,
                ),
                "actorUrl": getattr(community, "actor_url"),
                "aliasUrl": f"{origin}/c/{getattr(community, 'slug')}",
                "followersUrl": getattr(community, "followers_url"),
                # Stage 1 keeps participant types explicit. A later read model
                # may add a combined total, but the payload should not imply
                # that remote and local participants are the same stored state.
                "remoteSubscriberCount": len(community_remote_subscribers),
                "localSubscriberCount": local_subscriber_count,
                "followers": remote_subscriber_payloads,
            }
        )

    allowlist = _normalize_host_list(getattr(settings, "federation_allowlist", []))
    bridge_follow_payloads = [
        {
            "communityActorUrl": getattr(follow, "community_actor_id"),
            "instanceHost": _hostname_from_url(getattr(follow, "community_actor_id", "")),
            "status": getattr(follow, "status"),
            "technicalDetails": {
                "communityInboxUrl": getattr(follow, "community_inbox_url"),
            },
        }
        for follow in bridge_follows
    ]

    return {
        "instance": {
            "title": "Discord/Fediverse Bridge Instance",
            "origin": origin,
            "bridgeActorUrl": f"{origin}/actors/{actor_identifier}",
            "registeredUserCount": len(registered_users),
            "localCommunityCount": len(community_payloads),
            "localCommunityFollowerCount": len(remote_subscribers),
            "bridgeActorFollowCount": len(bridge_follow_payloads),
        },
        "localCommunities": community_payloads,
        "bridgeActorFollows": bridge_follow_payloads,
        "federation": {
            "mode": "restricted_allowlist" if allowlist else "open",
            "allowlist": allowlist,
        },
        "credits": {
            "label": "Made with passion by Nachitima",
            "url": "https://nachitima.com",
        },
    }


def render_dashboard_html(payload_endpoint: str = "/dashboard/data") -> str:
    """Render the dashboard shell from the external HTML asset.

    The shell stays in a dedicated HTML file so the route layer does not keep
    large embedded CSS and JavaScript strings in Python source. Only the JSON
    endpoint placeholder is injected dynamically.
    """
    template = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    # The dashboard JSON route remains configurable from Python so tests can
    # exercise alternate route wiring without editing the static asset.
    return template.replace("__DASHBOARD_DATA_ENDPOINT__", payload_endpoint)


def _hostname_from_url(value: str | None) -> str | None:
    """Extract a lowercase hostname from a URL-like value."""
    if not value:
        return None
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return parsed.hostname.lower() if parsed.hostname else None


def _normalize_host_list(values: list[str]) -> list[str]:
    """Normalize allowlist entries as sorted lowercase hostnames."""
    hosts = {_hostname_from_url(value.strip()) for value in values if value.strip()}
    return sorted(host for host in hosts if host)


def _local_community_relay_handle(slug: str, origin_host: str) -> str:
    """Build the public local-community relay handle shown on the dashboard.

    The dashboard should expose the federation-facing handle operators and
    readers actually use, not only the internal slug path segment.
    """
    if not origin_host:
        return f"!{slug}"
    return f"!{slug}@{origin_host}"
