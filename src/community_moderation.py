"""Inbound local-community moderation checks for ActivityPub deliveries."""

from __future__ import annotations

from .activitypub_models import BridgeGatewayEvent
from .fediverse_identity import extract_remote_actor_handle_from_actor_url
from .local_communities.inbound_mapping import resolve_local_community_by_actor_url
from .models import CommunityActorBan
from .runtime import Runtime
from .user_bans import UserBanService


def find_local_community_actor_ban_for_event(
    event: BridgeGatewayEvent,
    runtime: Runtime,
) -> CommunityActorBan | None:
    """Return the effective global-first actor ban before inbound side effects."""
    community_actor_id = getattr(event, "community_actor_id", None)
    actor_url = getattr(event, "actor_id", None)
    if not community_actor_id or not actor_url:
        return None
    local_community = resolve_local_community_by_actor_url(runtime.database, community_actor_id)
    if local_community is None:
        return None
    actor_handle = extract_remote_actor_handle_from_actor_url(actor_url)
    decision = UserBanService(database=runtime.database, settings=getattr(runtime, "settings", None)).check_activitypub_actor(
        local_community_id=int(local_community.id), actor_url=actor_url, actor_handle=actor_handle
    )
    ban = decision.ban
    if ban is not None and getattr(ban, "actor_url", None) is None:
        # Cache the observed URL only after a handle match; no network lookup is introduced.
        runtime.database.community_actor_bans.fill_actor_url_if_missing(
            ban_id=int(getattr(ban, "id")), actor_url=actor_url
        )
    return ban
