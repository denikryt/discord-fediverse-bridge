"""ActivityPub renderers for local-community federation relay.

Renderers translate a durable inbound source activity into an outbound activity
that can be signed by the local community actor. The module owns only payload
shape; target selection, persistence, retry policy, and transport stay outside.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

PUBLIC_COLLECTION = "https://www.w3.org/ns/activitystreams#Public"
SUPPORTED_DELIVERY_PROFILES = {
    "threadiverse_group",
    "mastodon_compat",
    "generic_activitypub",
}


def render_local_community_relay_activity(
    *,
    source_activity_json: dict,
    community_actor_url: str,
    community_slug: str,
    delivery_profile: str,
) -> dict:
    """Render one outbound community Announce for a target delivery profile.

    The first implementation intentionally emits the same preserve-and-announce
    shape for every profile. Separate branches keep the compatibility seam in
    place so Mastodon-specific rendering can later change without touching
    fanout persistence or gateway transport.
    """
    if delivery_profile not in SUPPORTED_DELIVERY_PROFILES:
        delivery_profile = "generic_activitypub"

    if delivery_profile == "threadiverse_group":
        return _render_announce(source_activity_json, community_actor_url, community_slug)
    if delivery_profile == "mastodon_compat":
        return _render_announce(source_activity_json, community_actor_url, community_slug)
    return _render_announce(source_activity_json, community_actor_url, community_slug)


def _render_announce(source_activity_json: dict, community_actor_url: str, community_slug: str) -> dict:
    """Build a community-owned Announce without mutating the source activity."""
    # Deep-copying protects the persisted source JSON from accidental renderer
    # mutation while preserving the original actor and object attribution.
    source_activity = deepcopy(source_activity_json)
    announce_id = (
        f"{community_actor_url.rstrip('/')}/activities/announce/"
        f"{datetime.now(timezone.utc).timestamp():.6f}-{uuid4().hex}"
    )
    followers_url = f"{community_actor_url.rstrip('/')}/followers"
    return {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": announce_id,
        "type": "Announce",
        "actor": community_actor_url,
        "to": [PUBLIC_COLLECTION],
        "cc": [followers_url],
        "object": source_activity,
    }
