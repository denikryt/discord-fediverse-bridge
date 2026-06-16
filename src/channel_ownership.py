"""Cross-domain lookup for Discord channels already owned by bridge modes."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ChannelCommunitySubscription, LocalCommunity, LocalSubscriber, PersonalFeedBinding


@dataclass(frozen=True)
class ChannelOwner:
    """Describe one non-secret durable channel claim."""

    kind: str
    row_id: int
    discord_channel_id: int
    discord_guild_id: int | None


def resolve_channel_owner(session: Session, channel_id: int) -> ChannelOwner | None:
    """Return the first durable owner for a channel across current bridge domains."""
    queries = (
        ("remote_subscription", ChannelCommunitySubscription, ChannelCommunitySubscription.discord_channel_id, ChannelCommunitySubscription.discord_guild_id),
        ("local_community_host", LocalCommunity, LocalCommunity.discord_forum_channel_id, LocalCommunity.discord_guild_id),
        ("local_subscriber", LocalSubscriber, LocalSubscriber.discord_channel_id, LocalSubscriber.discord_guild_id),
        ("personal_feed", PersonalFeedBinding, PersonalFeedBinding.discord_channel_id, PersonalFeedBinding.discord_guild_id),
    )
    for kind, model, channel_column, guild_column in queries:
        row = session.scalar(select(model).where(channel_column == channel_id))
        if row is not None:
            return ChannelOwner(kind, row.id, channel_id, getattr(row, guild_column.key))
    return None
