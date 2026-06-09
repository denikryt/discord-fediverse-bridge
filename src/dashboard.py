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

from .bridge_policy import PolicyType
from .project_version import APP_VERSION

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DASHBOARD_HTML_PATH = WEB_DIR / "dashboard.html"
UNKNOWN_GUILD = "Unknown guild"
UNKNOWN_FORUM_CHANNEL = "Unknown forum channel"


def build_dashboard_payload(runtime: Any) -> dict[str, object]:
    """Build the public dashboard payload from safe runtime state."""
    settings = runtime.settings
    origin = str(getattr(settings, "normalized_fedify_origin", settings.fedify_origin)).rstrip("/")
    origin_host = _hostname_from_url(origin) or ""
    actor_identifier = getattr(settings, "fedify_actor_identifier", "bridge")
    database = runtime.database
    snapshot = runtime.bridge_policy_service.snapshot()
    local_communities = [
        row for row in database.local_communities.list_local_communities()
        if snapshot.is_discord_guild_allowed(getattr(row, "discord_guild_id", None))
    ]
    registered_users = database.users.list_users()
    remote_subscribers = [
        row for row in database.remote_subscribers.list_remote_subscribers_for_all(status="accepted")
        if snapshot.federation_decision(
            getattr(row, "remote_actor_id", None) or getattr(row, "remote_inbox_url", "")
        ).allowed
    ]
    bridge_follows = [
        row for row in database.bridge_actor_follows.list_bridge_actor_follows()
        if snapshot.federation_decision(getattr(row, "community_actor_id", "")).allowed
    ]
    accepted_remote_subscriptions = [
        row for row in database.remote_subscriptions.list_subscriptions(status="accepted")
        if snapshot.is_discord_guild_allowed(getattr(row, "discord_guild_id", None))
        and snapshot.federation_decision(getattr(row, "lemmy_community_actor_id", "")).allowed
    ]
    active_local_subscribers = [
        row for row in database.local_subscribers.list_all_local_subscribers(status="active")
        if snapshot.is_discord_guild_allowed(getattr(row, "discord_guild_id", None))
    ]
    invite_publications = [
        row for row in database.guild_invite_publications.list_publications()
        if snapshot.is_discord_guild_allowed(getattr(row, "discord_guild_id", None))
    ]

    guild_snapshots, channel_snapshots = _load_discord_snapshots(
        database,
        local_communities=local_communities,
        remote_subscriptions=accepted_remote_subscriptions,
        local_subscribers=active_local_subscribers,
    )

    remote_subscribers_by_community: dict[int, list[object]] = defaultdict(list)
    for remote_subscriber in remote_subscribers:
        remote_subscribers_by_community[getattr(remote_subscriber, "local_community_id")].append(remote_subscriber)

    local_community_by_id = {getattr(community, "id"): community for community in local_communities}
    community_payloads = []
    guild_buckets: dict[int | None, dict[str, object]] = {}

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
        local_subscriber_count = database.local_subscribers.count_local_subscribers(getattr(community, "id"))
        host_discord = _discord_labels_for_row(
            guild_id=getattr(community, "discord_guild_id", None),
            channel_id=getattr(community, "discord_forum_channel_id", None),
            guild_snapshots=guild_snapshots,
            channel_snapshots=channel_snapshots,
        )
        relay_handle = _local_community_relay_handle(
            getattr(community, "slug"),
            origin_host,
        )
        community_payloads.append(
            {
                "slug": getattr(community, "slug"),
                "name": getattr(community, "display_name"),
                "description": getattr(community, "summary"),
                "relayHandle": relay_handle,
                "hostDiscord": host_discord,
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
        bucket = _guild_bucket(
            guild_buckets,
            guild_id=getattr(community, "discord_guild_id", None),
            channel_id=getattr(community, "discord_forum_channel_id", None),
            guild_snapshots=guild_snapshots,
            channel_snapshots=channel_snapshots,
        )
        bucket["hostedCommunities"].append(
            {
                "relayHandle": relay_handle,
                "forumChannelName": host_discord["forumChannelName"],
            }
        )

    for subscription in accepted_remote_subscriptions:
        bucket = _guild_bucket(
            guild_buckets,
            guild_id=getattr(subscription, "discord_guild_id", None),
            channel_id=getattr(subscription, "discord_channel_id", None),
            guild_snapshots=guild_snapshots,
            channel_snapshots=channel_snapshots,
        )
        channel_snapshot = channel_snapshots.get(getattr(subscription, "discord_channel_id", None))
        bucket["remoteSubscriptions"].append(
            {
                "forumChannelName": _channel_name(channel_snapshot),
                "communityHandle": _remote_subscription_label(subscription),
            }
        )

    for subscriber in active_local_subscribers:
        community = local_community_by_id.get(getattr(subscriber, "local_community_id", None))
        bucket = _guild_bucket(
            guild_buckets,
            guild_id=getattr(subscriber, "discord_guild_id", None),
            channel_id=getattr(subscriber, "discord_channel_id", None),
            guild_snapshots=guild_snapshots,
            channel_snapshots=channel_snapshots,
        )
        channel_snapshot = channel_snapshots.get(getattr(subscriber, "discord_channel_id", None))
        bucket["localSubscriptions"].append(
            {
                "forumChannelName": _channel_name(channel_snapshot),
                "communityHandle": _local_subscriber_label(community, origin_host),
            }
        )

    allowlist = [
        entry.subject
        for entry in snapshot.list_effective_entries(PolicyType.FEDERATION_ALLOW)
        if snapshot.federation_decision(entry.subject).allowed
    ]
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

    publications_by_guild = {int(row.discord_guild_id): row for row in invite_publications}
    for guild_id, bucket in guild_buckets.items():
        publication = publications_by_guild.get(guild_id) if guild_id is not None else None
        bucket["inviteUrl"] = str(publication.invite_url) if publication is not None else None

    return {
        "instance": {
            "title": "Discord/Fediverse Bridge Instance",
            "version": APP_VERSION,
            "origin": origin,
            "bridgeActorUrl": f"{origin}/actors/{actor_identifier}",
            "registeredUserCount": len(registered_users),
            "localCommunityCount": len(community_payloads),
            "localCommunityFollowerCount": len(remote_subscribers),
            "bridgeActorFollowCount": len(bridge_follow_payloads),
        },
        "localCommunities": community_payloads,
        "discordGuilds": _finalize_guild_buckets(guild_buckets),
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


def _load_discord_snapshots(
    database: Any,
    *,
    local_communities: list[object],
    remote_subscriptions: list[object],
    local_subscribers: list[object],
) -> tuple[dict[int, object], dict[int, object]]:
    """Load all Discord snapshots required for one dashboard payload."""
    guild_ids: set[int] = set()
    channel_ids: set[int] = set()
    for row, guild_attr, channel_attr in (
        *((row, "discord_guild_id", "discord_forum_channel_id") for row in local_communities),
        *((row, "discord_guild_id", "discord_channel_id") for row in remote_subscriptions),
        *((row, "discord_guild_id", "discord_channel_id") for row in local_subscribers),
    ):
        guild_id = getattr(row, guild_attr, None)
        channel_id = getattr(row, channel_attr, None)
        if guild_id is not None:
            guild_ids.add(int(guild_id))
        if channel_id is not None:
            channel_ids.add(int(channel_id))

    channel_rows = database.discord_directory.list_channel_snapshots(sorted(channel_ids))
    for snapshot in channel_rows:
        # Older subscription rows may have NULL guild ids. Channel snapshots can
        # still recover the guild grouping without exposing the numeric id.
        if getattr(snapshot, "discord_guild_id", None) is not None:
            guild_ids.add(int(getattr(snapshot, "discord_guild_id")))
    guild_rows = database.discord_directory.list_guild_snapshots(sorted(guild_ids))
    return (
        {int(getattr(row, "discord_guild_id")): row for row in guild_rows},
        {int(getattr(row, "discord_channel_id")): row for row in channel_rows},
    )


def _guild_bucket(
    buckets: dict[int | None, dict[str, object]],
    *,
    guild_id: int | None,
    channel_id: int | None,
    guild_snapshots: dict[int, object],
    channel_snapshots: dict[int, object],
) -> dict[str, object]:
    """Return the public bucket for one Discord guild placement."""
    key = guild_id
    channel_snapshot = channel_snapshots.get(channel_id) if channel_id is not None else None
    if key is None and channel_snapshot is not None:
        key = getattr(channel_snapshot, "discord_guild_id", None)
    key = int(key) if key is not None else None
    if key not in buckets:
        guild_snapshot = guild_snapshots.get(key) if key is not None else None
        buckets[key] = {
            "_sortKey": key,
            "guildName": _guild_name(guild_snapshot),
            "hostedCommunities": [],
            "remoteSubscriptions": [],
            "localSubscriptions": [],
            "inviteUrl": None,
        }
    return buckets[key]


def _finalize_guild_buckets(buckets: dict[int | None, dict[str, object]]) -> list[dict[str, object]]:
    """Sort and strip private bucket metadata before serialization."""
    sorted_buckets = sorted(
        buckets.values(),
        key=lambda bucket: (
            str(bucket["guildName"]).lower(),
            str(bucket.get("_sortKey") if bucket.get("_sortKey") is not None else ""),
        ),
    )
    result = []
    for bucket in sorted_buckets:
        bucket["hostedCommunities"] = sorted(
            bucket["hostedCommunities"],
            key=lambda row: (str(row["relayHandle"]).lower(), str(row["forumChannelName"]).lower()),
        )
        bucket["remoteSubscriptions"] = sorted(
            bucket["remoteSubscriptions"],
            key=lambda row: (str(row["communityHandle"]).lower(), str(row["forumChannelName"]).lower()),
        )
        bucket["localSubscriptions"] = sorted(
            bucket["localSubscriptions"],
            key=lambda row: (str(row["communityHandle"]).lower(), str(row["forumChannelName"]).lower()),
        )
        result.append({key: value for key, value in bucket.items() if key != "_sortKey"})
    return result


def _discord_labels_for_row(
    *,
    guild_id: int | None,
    channel_id: int | None,
    guild_snapshots: dict[int, object],
    channel_snapshots: dict[int, object],
) -> dict[str, str]:
    """Return public Discord labels for one hosted community row."""
    guild_snapshot = guild_snapshots.get(guild_id) if guild_id is not None else None
    channel_snapshot = channel_snapshots.get(channel_id) if channel_id is not None else None
    if guild_snapshot is None and channel_snapshot is not None:
        snapshot_guild_id = getattr(channel_snapshot, "discord_guild_id", None)
        guild_snapshot = guild_snapshots.get(snapshot_guild_id) if snapshot_guild_id is not None else None
    return {
        "guildName": _guild_name(guild_snapshot),
        "forumChannelName": _channel_name(channel_snapshot),
    }


def _guild_name(snapshot: object | None) -> str:
    """Return a public guild label with a stable missing-snapshot fallback."""
    return str(getattr(snapshot, "guild_name", None) or UNKNOWN_GUILD)


def _channel_name(snapshot: object | None) -> str:
    """Return a public forum-channel label with a stable fallback."""
    return str(getattr(snapshot, "channel_name", None) or UNKNOWN_FORUM_CHANNEL)


def _remote_subscription_label(subscription: object) -> str:
    """Return the best public community label for one remote subscription."""
    return str(
        getattr(subscription, "community_handle", None)
        or getattr(subscription, "lemmy_community_name", None)
        or getattr(subscription, "lemmy_community_actor_id")
    )


def _local_subscriber_label(community: object | None, origin_host: str) -> str:
    """Return the bridge-facing community handle for one local subscriber."""
    if community is None:
        return "Unknown local community"
    return _local_community_relay_handle(getattr(community, "slug"), origin_host)


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
