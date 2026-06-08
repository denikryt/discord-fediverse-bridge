"""Transactional application service for management state changes.

Management command operations call this layer for successful mutations. Each
method combines one domain repository mutation with its audit insert in the same
SQLAlchemy transaction, without making repositories know audit vocabulary.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from sqlalchemy.orm import Session

from .db.repositories.community_actor_bans import BanActivationResult, CommunityActorBanRepository
from .db.repositories.local_communities import LocalCommunityRepository
from .db.repositories.guild_invite_publications import GuildInvitePublicationRepository
from .management_audit_recorder import ManagementAuditRecorder
from .models import CommunityActorBan, GuildInvitePublication, LocalCommunity


class ManagementActions:
    """Run state-changing management actions with transactional audit writes."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Iterator[Session]],
        local_communities: LocalCommunityRepository,
        community_actor_bans: CommunityActorBanRepository,
        management_audit: ManagementAuditRecorder,
        guild_invite_publications: GuildInvitePublicationRepository,
    ) -> None:
        """Initialise the service with repositories and an audit recorder."""
        self._session_factory = session_factory
        self._local_communities = local_communities
        self._community_actor_bans = community_actor_bans
        self._audit = management_audit
        self._guild_invite_publications = guild_invite_publications

    def create_local_community(
        self,
        *,
        discord_guild_id: int,
        discord_forum_channel_id: int,
        slug: str,
        display_name: str,
        summary: str | None,
        created_by_discord_user_id: str,
        actor_url: str,
        inbox_url: str,
        outbox_url: str,
        followers_url: str,
        public_key_pem: str,
        private_key_pem: str,
        status: str = "active",
    ) -> LocalCommunity:
        """Create a local-community row and its creation audit row atomically."""
        with self._session_factory() as session:
            community = self._local_communities.add_local_community(
                session,
                discord_guild_id=discord_guild_id,
                discord_forum_channel_id=discord_forum_channel_id,
                slug=slug,
                display_name=display_name,
                summary=summary,
                created_by_discord_user_id=created_by_discord_user_id,
                actor_url=actor_url,
                inbox_url=inbox_url,
                outbox_url=outbox_url,
                followers_url=followers_url,
                public_key_pem=public_key_pem,
                private_key_pem=private_key_pem,
                status=status,
            )
            self._audit.add_community_created(
                session,
                actor_discord_user_id=created_by_discord_user_id,
                community=community,
            )
            return community

    def update_local_community_settings(
        self,
        *,
        actor_discord_user_id: str,
        local_community_id: int,
        display_name: str,
        summary: str | None,
        status: str,
    ) -> LocalCommunity | None:
        """Update community settings and audit only changed slices atomically."""
        with self._session_factory() as session:
            update = self._local_communities.set_local_community_settings(
                session,
                local_community_id=local_community_id,
                display_name=display_name,
                summary=summary,
                status=status,
            )
            if update is None:
                return None
            self._audit.add_community_settings_changed(
                session,
                actor_discord_user_id=actor_discord_user_id,
                update=update,
            )
            return update.community

    def create_or_reactivate_ban(
        self,
        *,
        actor_discord_user_id: str,
        local_community_id: int | None,
        actor_handle: str,
        actor_url: str | None,
        target_discord_user_id: str | None = None,
        reason: str | None = None,
    ) -> BanActivationResult:
        """Create/reactivate a ban and audit the activation atomically."""
        with self._session_factory() as session:
            result = self._community_actor_bans.create_or_reactivate_active_ban(
                session,
                local_community_id=local_community_id,
                actor_handle=actor_handle,
                actor_url=actor_url,
                target_discord_user_id=target_discord_user_id,
                created_by_discord_user_id=actor_discord_user_id,
                reason=reason,
            )
            self._audit.add_ban_activation(
                session,
                actor_discord_user_id=actor_discord_user_id,
                result=result,
            )
            return result

    def remove_ban(
        self,
        *,
        actor_discord_user_id: str,
        local_community_id: int | None,
        actor_handle: str,
    ) -> CommunityActorBan | None:
        """Deactivate an active ban and audit the removal atomically."""
        with self._session_factory() as session:
            ban = self._community_actor_bans.deactivate_active_ban_by_handle_in_session(
                session,
                local_community_id=local_community_id,
                actor_handle=actor_handle,
            )
            if ban is None:
                return None
            self._audit.add_ban_removed(
                session,
                actor_discord_user_id=actor_discord_user_id,
                ban=ban,
            )
            return ban


    def replace_guild_invite_publication(self, *, discord_guild_id: int, discord_channel_id: int, invite_code: str, invite_url: str, actor_discord_user_id: str) -> tuple[GuildInvitePublication | None, GuildInvitePublication]:
        """Replace guild invite state and write its audit row atomically."""
        with self._session_factory() as session:
            before, after = self._guild_invite_publications.replace_in_session(
                session,
                discord_guild_id=discord_guild_id,
                discord_channel_id=discord_channel_id,
                invite_code=invite_code,
                invite_url=invite_url,
                published_by_discord_user_id=actor_discord_user_id,
            )
            self._audit.add_guild_invite_published(session, actor_discord_user_id=actor_discord_user_id, before=before, after=after)
            return before, after

    def remove_guild_invite_publication(self, *, discord_guild_id: int, actor_discord_user_id: str) -> GuildInvitePublication | None:
        """Delete guild invite state and write its audit row atomically."""
        with self._session_factory() as session:
            publication = self._guild_invite_publications.delete_in_session(session, discord_guild_id=discord_guild_id)
            if publication is not None:
                self._audit.add_guild_invite_removed(session, actor_discord_user_id=actor_discord_user_id, publication=publication)
            return publication
