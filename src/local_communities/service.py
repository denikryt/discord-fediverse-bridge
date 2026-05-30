"""Creation and persistence policy for Discord-backed local communities.

The service owns local-community registration and actor identity creation so
command handlers remain thin adapters and the gateway can stay a pure protocol
edge that only reads the stored identity later.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from ..db import Database
from ..registration_service import generate_rsa_keypair_pem

SLUG_PATTERN = re.compile(r"^[a-z0-9_-]+$")


class LocalCommunityError(Exception):
    """Signal that a local-community creation request violates policy."""


@dataclass(slots=True)
class CreatedLocalCommunity:
    """Describe one persisted local community and its public actor metadata."""

    slug: str
    display_name: str
    summary: str
    actor_url: str
    inbox_url: str
    outbox_url: str
    followers_url: str
    discord_forum_channel_id: int
    discord_guild_id: int


class LocalCommunityService:
    """Own local-community validation, URL building, and persistence."""

    def __init__(
        self,
        *,
        database: Database,
        base_url: str,
        keypair_generator: Callable[[], tuple[str, str]] | None = None,
    ) -> None:
        """Initialise the service with shared persistence and origin settings."""
        self.database = database
        self.base_url = base_url.rstrip("/")
        self.keypair_generator = keypair_generator or generate_rsa_keypair_pem

    def create_local_community(
        self,
        *,
        discord_guild_id: int,
        discord_forum_channel_id: int,
        slug: str,
        name: str,
        description: str,
        created_by_discord_user_id: str,
    ) -> CreatedLocalCommunity:
        """Create one Discord-backed local community with stable actor metadata.

        The caller id is required for newly created rows but is not interpreted
        here. Command operations own authorization policy; this service only
        preserves the id alongside the local-community actor metadata.
        """
        normalized_slug = slug.strip().lower()
        self.validate_slug(normalized_slug)
        normalized_name = name.strip()
        normalized_description = description.strip()
        if not normalized_name:
            raise LocalCommunityError("Community name is required.")
        if not normalized_description:
            raise LocalCommunityError("Community description is required.")
        if self.database.local_communities.get_local_community_by_slug(normalized_slug) is not None:
            raise LocalCommunityError("That community slug is already taken.")
        if (
            self.database.local_communities.get_local_community_by_forum_channel_id(discord_forum_channel_id)
            is not None
        ):
            raise LocalCommunityError("That forum channel is already bound to a local community.")

        actor_url, inbox_url, outbox_url, followers_url = self.build_actor_urls(normalized_slug)
        public_key_pem, private_key_pem = self.keypair_generator()
        created = self.database.local_communities.create_local_community(
            discord_guild_id=discord_guild_id,
            discord_forum_channel_id=discord_forum_channel_id,
            slug=normalized_slug,
            display_name=normalized_name,
            summary=normalized_description,
            created_by_discord_user_id=created_by_discord_user_id,
            actor_url=actor_url,
            inbox_url=inbox_url,
            outbox_url=outbox_url,
            followers_url=followers_url,
            public_key_pem=public_key_pem,
            private_key_pem=private_key_pem,
        )
        return CreatedLocalCommunity(
            slug=created.slug,
            display_name=created.display_name,
            summary=created.summary,
            actor_url=created.actor_url,
            inbox_url=created.inbox_url,
            outbox_url=created.outbox_url,
            followers_url=created.followers_url,
            discord_forum_channel_id=created.discord_forum_channel_id,
            discord_guild_id=created.discord_guild_id,
        )

    def validate_slug(self, slug: str) -> None:
        """Validate the stable community slug used in URLs and handles."""
        if not slug:
            raise LocalCommunityError("Community slug is required.")
        if not SLUG_PATTERN.fullmatch(slug):
            raise LocalCommunityError(
                "Community slug must use only lowercase letters, numbers, underscores, or hyphens."
            )

    def build_actor_urls(self, slug: str) -> tuple[str, str, str, str]:
        """Build the canonical actor URLs for one local community slug."""
        actor_url = f"{self.base_url}/communities/{slug}"
        return (
            actor_url,
            f"{actor_url}/inbox",
            f"{actor_url}/outbox",
            f"{actor_url}/followers",
        )
