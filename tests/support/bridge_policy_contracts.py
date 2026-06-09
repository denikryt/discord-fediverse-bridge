"""Typed executable contracts for bridge policy evaluation and management."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

BridgePolicyAction = Literal[
    "federation_decision",
    "guild_decision",
    "add",
    "remove",
]
CallerRole = Literal["super_admin", "unauthorized", "not_applicable"]
ExistingDynamicState = Literal["absent", "active", "inactive"]
GuildContext = Literal["allowed", "blocked", "dm"]


@dataclass(frozen=True, slots=True)
class BridgePolicyExpected:
    """Declare public outcome, persistence, and audit effects independently."""

    reason: str
    applied: bool | None = None
    decision_allowed: bool | None = None
    decision_reason: str | None = None
    active_entries: tuple[tuple[str, str], ...] = ()
    inactive_entries: tuple[tuple[str, str], ...] = ()
    audit_events: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class BridgePolicyContractCase:
    """Describe one bridge-policy action under a concrete effective state."""

    id: str
    action: BridgePolicyAction
    caller_role: CallerRole
    policy_type: str
    subject: str
    bootstrap_allow: tuple[str, ...] = ()
    bootstrap_block: tuple[str, ...] = ()
    existing_dynamic_state: ExistingDynamicState = "absent"
    guild_context: GuildContext = "allowed"
    expected: BridgePolicyExpected = BridgePolicyExpected(reason="")


BRIDGE_POLICY_CONTRACT_CASES: tuple[BridgePolicyContractCase, ...] = (
    BridgePolicyContractCase(
        id="federation.block_overrides_allow",
        action="federation_decision",
        caller_role="not_applicable",
        policy_type="federation-block",
        subject="remote.example",
        bootstrap_allow=("remote.example",),
        existing_dynamic_state="active",
        expected=BridgePolicyExpected(
            reason="evaluated",
            decision_allowed=False,
            decision_reason="blocklisted",
            active_entries=(("federation_block", "remote.example"),),
        ),
    ),
    BridgePolicyContractCase(
        id="federation.empty_allowlist_open",
        action="federation_decision",
        caller_role="not_applicable",
        policy_type="federation-allow",
        subject="open.example",
        expected=BridgePolicyExpected(
            reason="evaluated",
            decision_allowed=True,
            decision_reason="allowed",
        ),
    ),
    BridgePolicyContractCase(
        id="federation.nonempty_allowlist_restricts",
        action="federation_decision",
        caller_role="not_applicable",
        policy_type="federation-allow",
        subject="other.example",
        bootstrap_allow=("allowed.example",),
        expected=BridgePolicyExpected(
            reason="evaluated",
            decision_allowed=False,
            decision_reason="not_allowlisted",
        ),
    ),
    BridgePolicyContractCase(
        id="guild.block_overrides_allow",
        action="guild_decision",
        caller_role="not_applicable",
        policy_type="guild-block",
        subject="200",
        bootstrap_allow=("200",),
        bootstrap_block=("200",),
        expected=BridgePolicyExpected(
            reason="evaluated",
            decision_allowed=False,
            decision_reason="guild_not_allowed",
        ),
    ),
    BridgePolicyContractCase(
        id="manage.super_admin.add_created",
        action="add",
        caller_role="super_admin",
        policy_type="federation-block",
        subject="Remote.Example.",
        expected=BridgePolicyExpected(
            applied=True,
            reason="created",
            active_entries=(("federation_block", "remote.example"),),
            audit_events=(("bridge_policy.added", "success"),),
        ),
    ),
    BridgePolicyContractCase(
        id="manage.unauthorized.forbidden",
        action="add",
        caller_role="unauthorized",
        policy_type="guild-block",
        subject="200",
        expected=BridgePolicyExpected(
            applied=False,
            reason="not_effective_super_admin",
            audit_events=(("bridge_policy.manage_forbidden", "forbidden"),),
        ),
    ),
    BridgePolicyContractCase(
        id="manage.bootstrap_immutable",
        action="remove",
        caller_role="super_admin",
        policy_type="federation-block",
        subject="blocked.example",
        bootstrap_block=("blocked.example",),
        expected=BridgePolicyExpected(
            applied=False,
            reason="bootstrap_entry_immutable",
        ),
    ),
    BridgePolicyContractCase(
        id="manage.duplicate_active_rejected",
        action="add",
        caller_role="super_admin",
        policy_type="federation-block",
        subject="blocked.example",
        existing_dynamic_state="active",
        expected=BridgePolicyExpected(
            applied=False,
            reason="invalid_policy_state",
            active_entries=(("federation_block", "blocked.example"),),
        ),
    ),
    BridgePolicyContractCase(
        id="manage.inactive_reactivated",
        action="add",
        caller_role="super_admin",
        policy_type="federation-block",
        subject="blocked.example",
        existing_dynamic_state="inactive",
        expected=BridgePolicyExpected(
            applied=True,
            reason="reactivated",
            active_entries=(("federation_block", "blocked.example"),),
            audit_events=(("bridge_policy.reactivated", "success"),),
        ),
    ),
    BridgePolicyContractCase(
        id="manage.active_removed",
        action="remove",
        caller_role="super_admin",
        policy_type="federation-block",
        subject="blocked.example",
        existing_dynamic_state="active",
        expected=BridgePolicyExpected(
            applied=True,
            reason="removed",
            inactive_entries=(("federation_block", "blocked.example"),),
            audit_events=(("bridge_policy.removed", "success"),),
        ),
    ),
    BridgePolicyContractCase(
        id="manage.invalid_subject_rejected",
        action="add",
        caller_role="super_admin",
        policy_type="federation-block",
        subject="bad host",
        expected=BridgePolicyExpected(
            applied=False,
            reason="invalid_subject",
        ),
    ),
    BridgePolicyContractCase(
        id="manage.blocked_guild_rejected",
        action="add",
        caller_role="super_admin",
        policy_type="federation-block",
        subject="remote.example",
        guild_context="blocked",
        expected=BridgePolicyExpected(
            applied=False,
            reason="guild_not_allowed",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class BridgePolicyRequiredRule:
    """Declare one reviewable bridge-policy rule and representing cases."""

    id: str
    description: str
    represented_by: tuple[str, ...]


REQUIRED_BRIDGE_POLICY_RULES: tuple[BridgePolicyRequiredRule, ...] = (
    BridgePolicyRequiredRule("federation_block_precedence", "Federation block overrides allow.", ("federation.block_overrides_allow",)),
    BridgePolicyRequiredRule("empty_federation_allowlist_open", "Empty federation allowlist preserves open mode.", ("federation.empty_allowlist_open",)),
    BridgePolicyRequiredRule("nonempty_federation_allowlist_restricts", "Non-empty federation allowlist denies unrelated hosts.", ("federation.nonempty_allowlist_restricts",)),
    BridgePolicyRequiredRule("guild_block_precedence", "Guild block overrides guild allow.", ("guild.block_overrides_allow",)),
    BridgePolicyRequiredRule("super_admin_add", "Effective super-admin can add dynamic policy.", ("manage.super_admin.add_created",)),
    BridgePolicyRequiredRule("unauthorized_mutation_forbidden", "Non-super-admin mutation is forbidden and audited.", ("manage.unauthorized.forbidden",)),
    BridgePolicyRequiredRule("bootstrap_immutable", "Bootstrap entries cannot be changed dynamically.", ("manage.bootstrap_immutable",)),
    BridgePolicyRequiredRule("duplicate_active_rejected", "Duplicate active dynamic entry is rejected.", ("manage.duplicate_active_rejected",)),
    BridgePolicyRequiredRule("inactive_reactivated", "Inactive dynamic entry is reactivated.", ("manage.inactive_reactivated",)),
    BridgePolicyRequiredRule("active_removed", "Active dynamic entry can be removed.", ("manage.active_removed",)),
    BridgePolicyRequiredRule("invalid_subject_rejected", "Invalid policy subjects do not mutate state.", ("manage.invalid_subject_rejected",)),
    BridgePolicyRequiredRule("blocked_guild_rejected", "Blocked guild context cannot mutate policy.", ("manage.blocked_guild_rejected",)),
)
