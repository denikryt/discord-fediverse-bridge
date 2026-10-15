"""Persistence access for current personal-feed bindings."""

from __future__ import annotations

from sqlalchemy import select

from ...models import PersonalFeedBinding, User
from .base import BaseRepository
from .platform_surfaces import discord_claim_query


class PersonalFeedBindingRepository(BaseRepository):
    """Read current personal-feed ownership without owning mutation transactions."""

    def get_by_user_id(self, user_id: int) -> PersonalFeedBinding | None:
        """Return the current binding for one local User Actor."""
        with self.session() as session:
            return session.scalar(
                select(PersonalFeedBinding).where(
                    PersonalFeedBinding.user_id == user_id
                )
            )

    def get_by_discord_user_id(
        self, discord_user_id: str
    ) -> PersonalFeedBinding | None:
        """Resolve one binding through the durable Discord-to-User identity."""
        with self.session() as session:
            return session.scalar(
                discord_claim_query(PersonalFeedBinding, "personal_feed_binding_id")
                .join(User, User.id == PersonalFeedBinding.user_id)
                .where(User.discord_user_id == str(discord_user_id))
            )

    def get_by_channel_id(self, channel_id: int) -> PersonalFeedBinding | None:
        """Return the binding that currently owns one Discord channel."""
        with self.session() as session:
            return session.scalar(
                discord_claim_query(
                    PersonalFeedBinding, "personal_feed_binding_id"
                ).where(PersonalFeedBinding.discord_channel_id == channel_id)
            )

    def get_by_guild_id(self, guild_id: int) -> PersonalFeedBinding | None:
        """Return the single personal-feed binding reserved in one guild."""
        with self.session() as session:
            return session.scalar(
                discord_claim_query(
                    PersonalFeedBinding, "personal_feed_binding_id"
                ).where(PersonalFeedBinding.discord_guild_id == guild_id)
            )

    def is_current_channel_for_user(self, user_id: int, channel_id: int) -> bool:
        """Return whether the channel is the user's current active binding."""
        binding = self.get_by_channel_id(channel_id)
        return (
            binding is not None
            and binding.user_id == user_id
            and binding.discord_channel_id == channel_id
        )
