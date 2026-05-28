"""Discord guild/channel snapshot persistence for public dashboard labels.

The dashboard must not call Discord APIs while serving public HTTP requests.
This repository stores last-known guild and forum-channel names captured from
command handlers and bot startup refreshes so dashboard rendering stays purely
DB-backed and bounded.
"""

from __future__ import annotations

from sqlalchemy import select

from ...models import DiscordChannelSnapshot, DiscordGuildSnapshot
from .base import BaseRepository


class DiscordDirectoryRepository(BaseRepository):
    """Persist last-known Discord guild and channel display-name snapshots."""

    def upsert_guild_snapshot(
        self,
        *,
        discord_guild_id: int,
        guild_name: str,
    ) -> DiscordGuildSnapshot:
        """Create or refresh one guild snapshot by Discord guild id."""
        with self.session() as session:
            snapshot = session.scalar(
                select(DiscordGuildSnapshot).where(
                    DiscordGuildSnapshot.discord_guild_id == discord_guild_id
                )
            )
            if snapshot is None:
                snapshot = DiscordGuildSnapshot(
                    discord_guild_id=discord_guild_id,
                    guild_name=guild_name,
                )
                session.add(snapshot)
            else:
                # Rename convergence is intentional: command paths and startup
                # refreshes update the same row instead of duplicating labels.
                snapshot.guild_name = guild_name
            session.flush()
            return snapshot

    def upsert_channel_snapshot(
        self,
        *,
        discord_channel_id: int,
        discord_guild_id: int | None,
        channel_name: str,
        channel_type: str,
    ) -> DiscordChannelSnapshot:
        """Create or refresh one Discord channel snapshot by channel id."""
        with self.session() as session:
            snapshot = session.scalar(
                select(DiscordChannelSnapshot).where(
                    DiscordChannelSnapshot.discord_channel_id == discord_channel_id
                )
            )
            if snapshot is None:
                snapshot = DiscordChannelSnapshot(
                    discord_channel_id=discord_channel_id,
                    discord_guild_id=discord_guild_id,
                    channel_name=channel_name,
                    channel_type=channel_type,
                )
                session.add(snapshot)
            else:
                # Channel ids are stable while names and parent guild metadata
                # can change; update all public-readable fields on each capture.
                snapshot.discord_guild_id = discord_guild_id
                snapshot.channel_name = channel_name
                snapshot.channel_type = channel_type
            session.flush()
            return snapshot

    def get_guild_snapshot(self, discord_guild_id: int) -> DiscordGuildSnapshot | None:
        """Load one guild snapshot by Discord guild id."""
        with self.session() as session:
            return session.scalar(
                select(DiscordGuildSnapshot).where(
                    DiscordGuildSnapshot.discord_guild_id == discord_guild_id
                )
            )

    def get_channel_snapshot(self, discord_channel_id: int) -> DiscordChannelSnapshot | None:
        """Load one channel snapshot by Discord channel id."""
        with self.session() as session:
            return session.scalar(
                select(DiscordChannelSnapshot).where(
                    DiscordChannelSnapshot.discord_channel_id == discord_channel_id
                )
            )

    def list_guild_snapshots(self, discord_guild_ids: list[int]) -> list[DiscordGuildSnapshot]:
        """Load guild snapshots for a bounded set of Discord guild ids."""
        if not discord_guild_ids:
            return []
        with self.session() as session:
            return list(
                session.scalars(
                    select(DiscordGuildSnapshot).where(
                        DiscordGuildSnapshot.discord_guild_id.in_(set(discord_guild_ids))
                    )
                )
            )

    def list_channel_snapshots(self, discord_channel_ids: list[int]) -> list[DiscordChannelSnapshot]:
        """Load channel snapshots for a bounded set of Discord channel ids."""
        if not discord_channel_ids:
            return []
        with self.session() as session:
            return list(
                session.scalars(
                    select(DiscordChannelSnapshot).where(
                        DiscordChannelSnapshot.discord_channel_id.in_(set(discord_channel_ids))
                    )
                )
            )
