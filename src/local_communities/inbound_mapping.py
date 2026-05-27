"""Inbound routing helpers for local-community ActivityPub deliveries.

The handler boundary decides whether an inbound event belongs to a local
community we own. This module keeps those local-community-specific routing
lookups separate from the runtime orchestration.
"""

from __future__ import annotations

from ..db import Database


def resolve_local_community_by_actor_url(database: Database, actor_url: str) -> object | None:
    """Return the owned local community for one actor URL, if the bridge owns it."""
    return database.local_communities.get_local_community_by_actor_url(actor_url)
