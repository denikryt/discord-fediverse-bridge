"""Persistence helpers for the current public guild invite publication."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import GuildInvitePublication
from .base import BaseRepository


class GuildInvitePublicationRepository(BaseRepository):
    """Read and mutate one published invite per Discord guild."""

    def get_by_guild_id(self, discord_guild_id: int) -> GuildInvitePublication | None:
        """Return the current publication for one guild."""
        with self.session() as session:
            return session.scalar(select(GuildInvitePublication).where(GuildInvitePublication.discord_guild_id == discord_guild_id))

    def list_publications(self) -> list[GuildInvitePublication]:
        """Return every current publication in stable guild order."""
        with self.session() as session:
            return list(session.scalars(select(GuildInvitePublication).order_by(GuildInvitePublication.discord_guild_id)))

    def replace_in_session(self, session: Session, *, discord_guild_id: int, discord_channel_id: int, invite_code: str, invite_url: str, published_by_discord_user_id: str) -> tuple[GuildInvitePublication | None, GuildInvitePublication]:
        """Replace the current guild publication inside a caller-owned transaction."""
        current = session.scalar(select(GuildInvitePublication).where(GuildInvitePublication.discord_guild_id == discord_guild_id))
        before = None
        if current is None:
            current = GuildInvitePublication(discord_guild_id=discord_guild_id, discord_channel_id=discord_channel_id, invite_code=invite_code, invite_url=invite_url, published_by_discord_user_id=published_by_discord_user_id)
            session.add(current)
        else:
            before = GuildInvitePublication(discord_guild_id=current.discord_guild_id, discord_channel_id=current.discord_channel_id, invite_code=current.invite_code, invite_url=current.invite_url, published_by_discord_user_id=current.published_by_discord_user_id)
            current.discord_channel_id = discord_channel_id
            current.invite_code = invite_code
            current.invite_url = invite_url
            current.published_by_discord_user_id = published_by_discord_user_id
        session.flush()
        return before, current

    def delete_in_session(self, session: Session, *, discord_guild_id: int) -> GuildInvitePublication | None:
        """Delete the current publication inside a caller-owned transaction."""
        current = session.scalar(select(GuildInvitePublication).where(GuildInvitePublication.discord_guild_id == discord_guild_id))
        if current is not None:
            session.delete(current)
            session.flush()
        return current
