"""Typed executable contract data for the bounded ban-management pilot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

BanAction = Literal["ban", "unban"]
CallerRole = Literal["owner", "super_admin", "unauthorized"]
BanScope = Literal["community", "global"]
CommunityState = Literal["enabled", "disabled", "missing"]
TargetKind = Literal["remote", "local"]
ExistingBanState = Literal["absent", "active", "removed"]


@dataclass(frozen=True, slots=True)
class BanExpected:
    """Declare visible operation and persisted-row outcomes independently."""

    applied: bool
    reason: str
    active_rows: int
    inactive_rows: int = 0
    target_discord_user_id: str | None = None


@dataclass(frozen=True, slots=True)
class BanContractCase:
    """Describe one named ban action under a concrete system state."""

    id: str
    action: BanAction
    caller_role: CallerRole
    scope: BanScope
    community_state: CommunityState
    target_kind: TargetKind
    existing_ban_state: ExistingBanState
    expected: BanExpected


BAN_CONTRACT_CASES: tuple[BanContractCase, ...] = (
    BanContractCase(
        id="owner.scoped.enabled.remote.absent.create",
        action="ban",
        caller_role="owner",
        scope="community",
        community_state="enabled",
        target_kind="remote",
        existing_ban_state="absent",
        expected=BanExpected(True, "created", 1),
    ),
    BanContractCase(
        id="super_admin.scoped.enabled.remote.absent.create",
        action="ban",
        caller_role="super_admin",
        scope="community",
        community_state="enabled",
        target_kind="remote",
        existing_ban_state="absent",
        expected=BanExpected(True, "created", 1),
    ),
    BanContractCase(
        id="unauthorized.scoped.enabled.remote.absent.forbidden",
        action="ban",
        caller_role="unauthorized",
        scope="community",
        community_state="enabled",
        target_kind="remote",
        existing_ban_state="absent",
        expected=BanExpected(False, "cannot_manage_community", 0),
    ),
    BanContractCase(
        id="super_admin.global.remote.absent.create",
        action="ban",
        caller_role="super_admin",
        scope="global",
        community_state="missing",
        target_kind="remote",
        existing_ban_state="absent",
        expected=BanExpected(True, "created", 1),
    ),
    BanContractCase(
        id="owner.global.remote.absent.forbidden",
        action="ban",
        caller_role="owner",
        scope="global",
        community_state="missing",
        target_kind="remote",
        existing_ban_state="absent",
        expected=BanExpected(False, "global_scope_requires_super_admin", 0),
    ),
    BanContractCase(
        id="owner.scoped.disabled.remote.absent.validation",
        action="ban",
        caller_role="owner",
        scope="community",
        community_state="disabled",
        target_kind="remote",
        existing_ban_state="absent",
        expected=BanExpected(False, "community_disabled", 0),
    ),
    BanContractCase(
        id="owner.scoped.missing.remote.absent.validation",
        action="ban",
        caller_role="owner",
        scope="community",
        community_state="missing",
        target_kind="remote",
        existing_ban_state="absent",
        expected=BanExpected(False, "unknown_or_inaccessible_community", 0),
    ),
    BanContractCase(
        id="owner.scoped.enabled.local.absent.create",
        action="ban",
        caller_role="owner",
        scope="community",
        community_state="enabled",
        target_kind="local",
        existing_ban_state="absent",
        expected=BanExpected(True, "created", 1, target_discord_user_id="777"),
    ),
    BanContractCase(
        id="owner.scoped.enabled.remote.active.duplicate",
        action="ban",
        caller_role="owner",
        scope="community",
        community_state="enabled",
        target_kind="remote",
        existing_ban_state="active",
        expected=BanExpected(False, "duplicate_active_ban", 1),
    ),
    BanContractCase(
        id="owner.scoped.enabled.remote.removed.reactivate",
        action="ban",
        caller_role="owner",
        scope="community",
        community_state="enabled",
        target_kind="remote",
        existing_ban_state="removed",
        expected=BanExpected(True, "reactivated", 1),
    ),
    BanContractCase(
        id="owner.scoped.enabled.remote.active.unban",
        action="unban",
        caller_role="owner",
        scope="community",
        community_state="enabled",
        target_kind="remote",
        existing_ban_state="active",
        expected=BanExpected(True, "unbanned", 0, inactive_rows=1),
    ),
    BanContractCase(
        id="owner.scoped.enabled.remote.absent.unban_validation",
        action="unban",
        caller_role="owner",
        scope="community",
        community_state="enabled",
        target_kind="remote",
        existing_ban_state="absent",
        expected=BanExpected(False, "no_active_ban", 0),
    ),
)
