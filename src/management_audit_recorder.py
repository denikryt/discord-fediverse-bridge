"""Semantic management-audit row construction.

This module is the only integration layer that translates management outcomes
into low-level audit rows. Operations call semantic methods, repositories stay
persistence-only, and successful state changes can still pass a caller-owned
session for atomic audit writes.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from .db.repositories.community_actor_bans import BanActivationResult
from .db.repositories.local_communities import LocalCommunitySettingsUpdate
from .db.repositories.management_audit_events import ManagementAuditEventRepository
from .management_audit import (
    ACTION_BAN_CREATED,
    ACTION_BAN_CREATE_FORBIDDEN,
    ACTION_BAN_REACTIVATED,
    ACTION_BAN_REMOVED,
    ACTION_BAN_REMOVE_FORBIDDEN,
    ACTION_COMMUNITY_CREATED,
    ACTION_COMMUNITY_CREATE_FORBIDDEN,
    ACTION_COMMUNITY_MANAGE_FORBIDDEN,
    ACTION_COMMUNITY_METADATA_UPDATED,
    ACTION_COMMUNITY_STATUS_CHANGED,
    ACTION_GUILD_INVITE_PUBLISHED,
    ACTION_GUILD_INVITE_REPLACED,
    ACTION_GUILD_INVITE_PUBLISH_FORBIDDEN,
    ACTION_GUILD_INVITE_REMOVED,
    ACTION_GUILD_INVITE_REMOVE_FORBIDDEN,
    ACTION_BRIDGE_POLICY_ADDED,
    ACTION_BRIDGE_POLICY_REACTIVATED,
    ACTION_BRIDGE_POLICY_REMOVED,
    ACTION_BRIDGE_POLICY_MANAGE_FORBIDDEN,
    REASON_COMMUNITY_DISABLED,
    REASON_NOT_OWNER_OR_SUPER_ADMIN,
    REASON_NOT_SUPER_ADMIN,
    REASON_MISSING_MANAGE_GUILD,
    REASON_NOT_EFFECTIVE_SUPER_ADMIN,
    RESULT_FORBIDDEN,
    RESULT_SUCCESS,
    TARGET_LOCAL_COMMUNITY,
    TARGET_REMOTE_ACTOR,
    TARGET_DISCORD_GUILD,
    TARGET_BRIDGE_POLICY_ENTRY,
    community_created_after,
)
from .models import CommunityActorBan, GuildInvitePublication, LocalCommunity, ManagementAuditEvent

_FAILED_PRECONDITION_REASON_CODES = {
    "cannot_manage_community": REASON_NOT_OWNER_OR_SUPER_ADMIN,
    "community_disabled": REASON_COMMUNITY_DISABLED,
}


class ManagementAuditRecorder:
    """Build management audit rows from semantic management outcomes.

    The recorder owns target semantics and reason-code mapping. It delegates only
    validated low-level insertion to ManagementAuditEventRepository.
    """

    def __init__(self, events: ManagementAuditEventRepository) -> None:
        """Initialise the recorder with the low-level event repository."""
        self._events = events

    def community_create_forbidden(
        self,
        *,
        actor_discord_user_id: str,
        attempted_slug: str,
    ) -> ManagementAuditEvent:
        """Record a rejected local-community creation authorization attempt."""
        return self._events.create_event(
            action=ACTION_COMMUNITY_CREATE_FORBIDDEN,
            result=RESULT_FORBIDDEN,
            actor_discord_user_id=actor_discord_user_id,
            target_type=TARGET_LOCAL_COMMUNITY,
            target_id=attempted_slug.strip().lower() or None,
            reason_code=REASON_NOT_SUPER_ADMIN,
        )

    def community_manage_forbidden(
        self,
        *,
        actor_discord_user_id: str,
        community: LocalCommunity,
    ) -> ManagementAuditEvent:
        """Record a rejected owner/super-admin community management attempt."""
        return self._events.create_event(
            action=ACTION_COMMUNITY_MANAGE_FORBIDDEN,
            result=RESULT_FORBIDDEN,
            actor_discord_user_id=actor_discord_user_id,
            local_community_id=community.id,
            target_type=TARGET_LOCAL_COMMUNITY,
            target_id=str(community.id),
            reason_code=REASON_NOT_OWNER_OR_SUPER_ADMIN,
        )

    def ban_create_global_forbidden(
        self, *, actor_discord_user_id: str
    ) -> ManagementAuditEvent:
        """Record an omitted-community attempt by a non-super-admin."""
        return self._events.create_event(
            action=ACTION_BAN_CREATE_FORBIDDEN,
            result=RESULT_FORBIDDEN,
            actor_discord_user_id=actor_discord_user_id,
            local_community_id=None,
            target_type=TARGET_REMOTE_ACTOR,
            target_id=None,
            reason_code=REASON_NOT_SUPER_ADMIN,
        )

    def ban_create_forbidden(
        self,
        *,
        actor_discord_user_id: str,
        community: LocalCommunity,
        failed_precondition: str,
    ) -> ManagementAuditEvent | None:
        """Record an audit-worthy rejected ban attempt for a known community."""
        reason_code = _FAILED_PRECONDITION_REASON_CODES.get(failed_precondition)
        if reason_code is None:
            # Non-audit-worthy validation failures intentionally stay quiet.
            return None
        return self._events.create_event(
            action=ACTION_BAN_CREATE_FORBIDDEN,
            result=RESULT_FORBIDDEN,
            actor_discord_user_id=actor_discord_user_id,
            local_community_id=community.id,
            target_type=TARGET_REMOTE_ACTOR,
            target_id=None,
            reason_code=reason_code,
        )

    def ban_remove_forbidden(
        self,
        *,
        actor_discord_user_id: str,
        community: LocalCommunity,
        failed_precondition: str,
    ) -> ManagementAuditEvent | None:
        """Record an audit-worthy rejected unban attempt for a known community."""
        reason_code = _FAILED_PRECONDITION_REASON_CODES.get(failed_precondition)
        if reason_code is None:
            # The v1 audit contract excludes invalid handles and no-active-ban.
            return None
        return self._events.create_event(
            action=ACTION_BAN_REMOVE_FORBIDDEN,
            result=RESULT_FORBIDDEN,
            actor_discord_user_id=actor_discord_user_id,
            local_community_id=community.id,
            target_type=TARGET_REMOTE_ACTOR,
            target_id=None,
            reason_code=reason_code,
        )

    def add_community_created(
        self,
        session: Session,
        *,
        actor_discord_user_id: str,
        community: LocalCommunity,
    ) -> ManagementAuditEvent:
        """Add the success audit row for a newly created local community."""
        return self._events.add_event(
            session,
            action=ACTION_COMMUNITY_CREATED,
            result=RESULT_SUCCESS,
            actor_discord_user_id=actor_discord_user_id,
            local_community_id=community.id,
            target_type=TARGET_LOCAL_COMMUNITY,
            target_id=str(community.id),
            after=community_created_after(community=community),
        )

    def add_community_settings_changed(
        self,
        session: Session,
        *,
        actor_discord_user_id: str,
        update: LocalCommunitySettingsUpdate,
    ) -> list[ManagementAuditEvent]:
        """Add metadata/status audit rows for changed community settings only."""
        events: list[ManagementAuditEvent] = []
        if update.metadata_after:
            events.append(
                self._events.add_event(
                    session,
                    action=ACTION_COMMUNITY_METADATA_UPDATED,
                    result=RESULT_SUCCESS,
                    actor_discord_user_id=actor_discord_user_id,
                    local_community_id=update.community.id,
                    target_type=TARGET_LOCAL_COMMUNITY,
                    target_id=str(update.community.id),
                    before=update.metadata_before,
                    after=update.metadata_after,
                )
            )
        if update.status_after:
            events.append(
                self._events.add_event(
                    session,
                    action=ACTION_COMMUNITY_STATUS_CHANGED,
                    result=RESULT_SUCCESS,
                    actor_discord_user_id=actor_discord_user_id,
                    local_community_id=update.community.id,
                    target_type=TARGET_LOCAL_COMMUNITY,
                    target_id=str(update.community.id),
                    before=update.status_before,
                    after=update.status_after,
                )
            )
        return events

    def add_ban_activation(
        self,
        session: Session,
        *,
        actor_discord_user_id: str,
        result: BanActivationResult,
    ) -> ManagementAuditEvent:
        """Add the success audit row for a ban creation or reactivation."""
        action = ACTION_BAN_CREATED if result.kind == "created" else ACTION_BAN_REACTIVATED
        return self._events.add_event(
            session,
            action=action,
            result=RESULT_SUCCESS,
            actor_discord_user_id=actor_discord_user_id,
            local_community_id=result.ban.local_community_id,
            target_type=TARGET_REMOTE_ACTOR,
            target_id=result.ban.actor_handle,
            before=result.before,
            after=result.after,
        )

    def add_ban_removed(
        self,
        session: Session,
        *,
        actor_discord_user_id: str,
        ban: CommunityActorBan,
    ) -> ManagementAuditEvent:
        """Add the success audit row for an active ban moved to inactive."""
        return self._events.add_event(
            session,
            action=ACTION_BAN_REMOVED,
            result=RESULT_SUCCESS,
            actor_discord_user_id=actor_discord_user_id,
            local_community_id=ban.local_community_id,
            target_type=TARGET_REMOTE_ACTOR,
            target_id=ban.actor_handle,
            before={"status": "active"},
            after={"status": "inactive"},
        )

    def bridge_policy_manage_forbidden(self, *, actor_discord_user_id: str) -> ManagementAuditEvent:
        """Record a policy-management attempt by a non-super-admin."""
        return self._events.create_event(
            action=ACTION_BRIDGE_POLICY_MANAGE_FORBIDDEN,
            result=RESULT_FORBIDDEN,
            actor_discord_user_id=actor_discord_user_id,
            target_type=TARGET_BRIDGE_POLICY_ENTRY,
            target_id=None,
            reason_code=REASON_NOT_EFFECTIVE_SUPER_ADMIN,
        )

    def add_bridge_policy_activation(self, session: Session, *, actor_discord_user_id: str, result: object) -> ManagementAuditEvent:
        """Audit creation or reactivation of one dynamic policy row."""
        action = ACTION_BRIDGE_POLICY_ADDED if getattr(result, "kind") == "created" else ACTION_BRIDGE_POLICY_REACTIVATED
        entry = getattr(result, "entry")
        return self._events.add_event(
            session,
            action=action,
            result=RESULT_SUCCESS,
            actor_discord_user_id=actor_discord_user_id,
            target_type=TARGET_BRIDGE_POLICY_ENTRY,
            target_id=f"{entry.policy_type}:{entry.normalized_subject}",
            before=getattr(result, "before"),
            after=getattr(result, "after"),
        )

    def add_bridge_policy_removed(self, session: Session, *, actor_discord_user_id: str, entry: object, removal_reason: str | None) -> ManagementAuditEvent:
        """Audit deactivation without mutating the stored activation reason."""
        after: dict[str, object] = {"status": "inactive"}
        if removal_reason is not None:
            after["removal_reason"] = removal_reason
        return self._events.add_event(
            session,
            action=ACTION_BRIDGE_POLICY_REMOVED,
            result=RESULT_SUCCESS,
            actor_discord_user_id=actor_discord_user_id,
            target_type=TARGET_BRIDGE_POLICY_ENTRY,
            target_id=f"{entry.policy_type}:{entry.normalized_subject}",
            before={"status": "active"},
            after=after,
        )

    def guild_invite_forbidden(self, *, actor_discord_user_id: str, discord_guild_id: int, removing: bool) -> ManagementAuditEvent:
        """Record a Manage Guild denial for invite publication or removal."""
        return self._events.create_event(
            action=ACTION_GUILD_INVITE_REMOVE_FORBIDDEN if removing else ACTION_GUILD_INVITE_PUBLISH_FORBIDDEN,
            result=RESULT_FORBIDDEN,
            actor_discord_user_id=actor_discord_user_id,
            target_type=TARGET_DISCORD_GUILD,
            target_id=str(discord_guild_id),
            reason_code=REASON_MISSING_MANAGE_GUILD,
        )

    def add_guild_invite_published(self, session: Session, *, actor_discord_user_id: str, before: GuildInvitePublication | None, after: GuildInvitePublication) -> ManagementAuditEvent:
        """Add first-publication or replacement audit in the caller transaction."""
        def snapshot(row: GuildInvitePublication | None) -> dict[str, object] | None:
            return None if row is None else {"discord_channel_id": int(row.discord_channel_id), "invite_code": str(row.invite_code)}
        return self._events.add_event(
            session,
            action=ACTION_GUILD_INVITE_REPLACED if before is not None else ACTION_GUILD_INVITE_PUBLISHED,
            result=RESULT_SUCCESS,
            actor_discord_user_id=actor_discord_user_id,
            target_type=TARGET_DISCORD_GUILD,
            target_id=str(after.discord_guild_id),
            before=snapshot(before),
            after=snapshot(after),
        )

    def add_guild_invite_removed(self, session: Session, *, actor_discord_user_id: str, publication: GuildInvitePublication) -> ManagementAuditEvent:
        """Add successful removal audit in the caller transaction."""
        return self._events.add_event(
            session,
            action=ACTION_GUILD_INVITE_REMOVED,
            result=RESULT_SUCCESS,
            actor_discord_user_id=actor_discord_user_id,
            target_type=TARGET_DISCORD_GUILD,
            target_id=str(publication.discord_guild_id),
            before={"discord_channel_id": int(publication.discord_channel_id), "invite_code": str(publication.invite_code)},
        )
