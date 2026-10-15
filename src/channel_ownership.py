"""Cross-domain lookup for Discord channels already owned by bridge modes."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    ChannelSurface,
    PlatformConnector,
    LocalCommunity,
    LocalSubscriber,
    PersonalFeedBinding,
)


@dataclass(frozen=True)
class ChannelOwner:
    """Describe one non-secret durable channel claim."""

    kind: str
    row_id: int
    discord_channel_id: int
    discord_guild_id: int | None


def resolve_channel_owner(session: Session, channel_id: int) -> ChannelOwner | None:
    """Return the first durable owner for a channel across current bridge domains."""
    if type(channel_id) is not int or channel_id <= 0:
        return None
    surface = session.scalar(
        select(ChannelSurface)
        .join(PlatformConnector)
        .where(
            PlatformConnector.connector_key == "discord-primary",
            PlatformConnector.platform == "discord",
            ChannelSurface.external_channel_id == str(channel_id),
        )
    )
    if surface is None or surface.role is None:
        return None
    # Typed FK is authoritative even for pending/failed remote owners.
    for field, kind in [
        ("remote_subscription_id", "remote_subscription"),
        ("local_community_id", "local_community_host"),
        ("local_subscriber_id", "local_subscriber"),
        ("personal_feed_binding_id", "personal_feed"),
    ]:
        row_id = getattr(surface, field)
        if row_id is not None:
            return ChannelOwner(
                kind,
                row_id,
                channel_id,
                (
                    int(surface.external_guild_id)
                    if surface.external_guild_id is not None
                    else None
                ),
            )
    return None
