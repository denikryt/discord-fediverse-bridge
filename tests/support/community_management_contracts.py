"""Typed executable contracts for local-community management."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CommunityAction = Literal["create", "edit"]
CallerRole = Literal["owner", "super_admin", "unauthorized"]
CommunityState = Literal["absent", "active", "disabled", "missing"]
GuildContext = Literal["same", "other", "dm"]


@dataclass(frozen=True, slots=True)
class CommunityManagementExpected:
    """Declare public, persistence, and audit effects independently."""

    applied: bool
    reason: str
    display_name: str | None = None
    summary: str | None = None
    status: str | None = None
    audit_events: tuple[tuple[str, str, str | None], ...] = ()


@dataclass(frozen=True, slots=True)
class CommunityManagementCase:
    """Describe one creation or edit contract under concrete state."""

    id: str
    action: CommunityAction
    caller_role: CallerRole
    community_state: CommunityState
    guild_context: GuildContext
    display_name: str
    summary: str | None
    requested_status: str
    slug: str = "cats"
    expected: CommunityManagementExpected = CommunityManagementExpected(False, "")


COMMUNITY_MANAGEMENT_CASES: tuple[CommunityManagementCase, ...] = (
    CommunityManagementCase(
        id="create.registered.valid.success",
        action="create",
        caller_role="owner",
        community_state="absent",
        guild_context="same",
        display_name="Cats",
        summary="A local community",
        requested_status="active",
        expected=CommunityManagementExpected(
            True,
            "created",
            display_name="Cats",
            summary="A local community",
            status="active",
            audit_events=(("community.created", "success", None),),
        ),
    ),
    CommunityManagementCase(
        id="create.invalid_slug.validation",
        action="create",
        caller_role="owner",
        community_state="absent",
        guild_context="same",
        display_name="Cats",
        summary=None,
        requested_status="active",
        slug="Invalid Slug",
        expected=CommunityManagementExpected(False, "validation_failed"),
    ),
    CommunityManagementCase(
        id="edit.owner.active.metadata_success",
        action="edit",
        caller_role="owner",
        community_state="active",
        guild_context="same",
        display_name="New Cats",
        summary="New summary",
        requested_status="active",
        expected=CommunityManagementExpected(
            True,
            "updated",
            display_name="New Cats",
            summary="New summary",
            status="active",
            audit_events=(("community.metadata_updated", "success", None),),
        ),
    ),
    CommunityManagementCase(
        id="edit.owner.active.disable_success",
        action="edit",
        caller_role="owner",
        community_state="active",
        guild_context="same",
        display_name="Cats",
        summary="Old summary",
        requested_status="disabled",
        expected=CommunityManagementExpected(
            True,
            "updated",
            display_name="Cats",
            summary="Old summary",
            status="disabled",
            audit_events=(("community.status_changed", "success", None),),
        ),
    ),
    CommunityManagementCase(
        id="edit.owner.disabled.enable_success",
        action="edit",
        caller_role="owner",
        community_state="disabled",
        guild_context="same",
        display_name="Cats",
        summary="Old summary",
        requested_status="active",
        expected=CommunityManagementExpected(
            True,
            "updated",
            display_name="Cats",
            summary="Old summary",
            status="active",
            audit_events=(("community.status_changed", "success", None),),
        ),
    ),
    CommunityManagementCase(
        id="edit.super_admin.cross_guild.success",
        action="edit",
        caller_role="super_admin",
        community_state="active",
        guild_context="other",
        display_name="Admin Cats",
        summary=None,
        requested_status="active",
        expected=CommunityManagementExpected(
            True,
            "updated",
            display_name="Admin Cats",
            summary=None,
            status="active",
            audit_events=(("community.metadata_updated", "success", None),),
        ),
    ),
    CommunityManagementCase(
        id="edit.unauthorized.same_guild.forbidden",
        action="edit",
        caller_role="unauthorized",
        community_state="active",
        guild_context="same",
        display_name="Bad Cats",
        summary=None,
        requested_status="disabled",
        expected=CommunityManagementExpected(
            False,
            "cannot_manage_community",
            display_name="Cats",
            summary="Old summary",
            status="active",
            audit_events=(("community.manage_forbidden", "forbidden", "not_owner_or_super_admin"),),
        ),
    ),
    CommunityManagementCase(
        id="edit.owner.cross_guild.inaccessible",
        action="edit",
        caller_role="owner",
        community_state="active",
        guild_context="other",
        display_name="New Cats",
        summary=None,
        requested_status="active",
        expected=CommunityManagementExpected(
            False,
            "unknown_or_inaccessible_community",
            display_name="Cats",
            summary="Old summary",
            status="active",
        ),
    ),
    CommunityManagementCase(
        id="edit.owner.dm.context_rejected",
        action="edit",
        caller_role="owner",
        community_state="active",
        guild_context="dm",
        display_name="New Cats",
        summary=None,
        requested_status="active",
        expected=CommunityManagementExpected(
            False,
            "missing_guild_context",
            display_name="Cats",
            summary="Old summary",
            status="active",
        ),
    ),
    CommunityManagementCase(
        id="edit.missing.validation",
        action="edit",
        caller_role="owner",
        community_state="missing",
        guild_context="same",
        display_name="New Cats",
        summary=None,
        requested_status="active",
        expected=CommunityManagementExpected(False, "unknown_or_inaccessible_community"),
    ),
    CommunityManagementCase(
        id="edit.invalid_status.validation",
        action="edit",
        caller_role="owner",
        community_state="active",
        guild_context="same",
        display_name="New Cats",
        summary="New summary",
        requested_status="archived",
        expected=CommunityManagementExpected(
            False,
            "invalid_status",
            display_name="Cats",
            summary="Old summary",
            status="active",
        ),
    ),
    CommunityManagementCase(
        id="edit.noop.success_without_audit",
        action="edit",
        caller_role="owner",
        community_state="active",
        guild_context="same",
        display_name="Cats",
        summary="Old summary",
        requested_status="active",
        expected=CommunityManagementExpected(
            True,
            "updated",
            display_name="Cats",
            summary="Old summary",
            status="active",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class CommunityManagementRequiredRule:
    """Declare one reviewable rule and its representing case IDs."""

    id: str
    description: str
    represented_by: tuple[str, ...]


REQUIRED_COMMUNITY_MANAGEMENT_RULES: tuple[CommunityManagementRequiredRule, ...] = (
    CommunityManagementRequiredRule("create_success", "Valid registered creation persists and audits.", ("create.registered.valid.success",)),
    CommunityManagementRequiredRule("create_validation", "Invalid creation input does not persist.", ("create.invalid_slug.validation",)),
    CommunityManagementRequiredRule("owner_metadata_edit", "Owner can update active community metadata.", ("edit.owner.active.metadata_success",)),
    CommunityManagementRequiredRule("owner_status_change", "Owner can disable and re-enable community lifecycle.", ("edit.owner.active.disable_success", "edit.owner.disabled.enable_success")),
    CommunityManagementRequiredRule("super_admin_cross_guild", "Super-admin can manage across guild context.", ("edit.super_admin.cross_guild.success",)),
    CommunityManagementRequiredRule("unauthorized_forbidden", "Unrelated caller is denied and audited.", ("edit.unauthorized.same_guild.forbidden",)),
    CommunityManagementRequiredRule("owner_cross_guild_hidden", "Owner cannot manage through another guild.", ("edit.owner.cross_guild.inaccessible",)),
    CommunityManagementRequiredRule("guild_context_required", "Management requires a guild context.", ("edit.owner.dm.context_rejected",)),
    CommunityManagementRequiredRule("missing_rejected", "Missing community is rejected without mutation.", ("edit.missing.validation",)),
    CommunityManagementRequiredRule("invalid_status_rejected", "Unsupported lifecycle status is rejected.", ("edit.invalid_status.validation",)),
    CommunityManagementRequiredRule("noop_success_no_audit", "No-op save succeeds without change audit.", ("edit.noop.success_without_audit",)),
)
