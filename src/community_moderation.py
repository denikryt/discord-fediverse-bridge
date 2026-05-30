"""Inbound local-community moderation checks for ActivityPub deliveries.

The gateway forwards valid deliveries to Python as before. This module resolves
only local bridge state and returns a ban decision before domain handlers create
Discord surfaces, subscriber rows, relay rows, or fanout side effects.
"""

from __future__ import annotations

from .activitypub_models import BridgeGatewayEvent
from .fediverse_identity import extract_remote_actor_handle_from_actor_url
from .local_communities.inbound_mapping import resolve_local_community_by_actor_url
from .models import CommunityActorBan
from .runtime import Runtime


def find_local_community_actor_ban_for_event(
    event: BridgeGatewayEvent,
    runtime: Runtime,
) -> CommunityActorBan | None:
    """Return the active scoped ban that should skip one inbound event.

    Matching is limited to events whose target local community can be resolved
    from the normalized event contract. The function is DB-only and string-only;
    no WebFinger or actor fetches are allowed on this hot path.
    """
    community_actor_id = getattr(event, "community_actor_id", None)
    actor_url = getattr(event, "actor_id", None)
    if not community_actor_id or not actor_url:
        return None

    local_community = resolve_local_community_by_actor_url(
        runtime.database,
        community_actor_id,
    )
    if local_community is None:
        return None

    actor_handle = extract_remote_actor_handle_from_actor_url(actor_url)
    ban = runtime.database.community_actor_bans.find_active_ban_for_actor(
        local_community_id=getattr(local_community, "id"),
        actor_url=actor_url,
        actor_handle=actor_handle,
    )
    if ban is not None and getattr(ban, "actor_url", None) is None:
        # A handle-created ban becomes more precise after the first observed
        # delivery from that actor, without adding network identity resolution.
        runtime.database.community_actor_bans.fill_actor_url_if_missing(
            ban_id=getattr(ban, "id"),
            actor_url=actor_url,
        )
    return ban
