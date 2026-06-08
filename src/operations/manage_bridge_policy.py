"""DiscordOps operation for adding and removing dynamic bridge policy entries."""

from __future__ import annotations

from dataclasses import dataclass, field

from discordops import Operation, Precondition

from ..bridge_policy import BridgePolicyService, BridgePolicySnapshot, PolicyType
from ..db import Database
from ..user_bans import BanDecision, UserBanService
from .common_preconditions import (
    GLOBAL_USER_NOT_BANNED,
    GUILD_POLICY_ALLOWS_COMMAND,
)

_TYPE_ALIASES = {
    "federation-allow": PolicyType.FEDERATION_ALLOW,
    "federation-block": PolicyType.FEDERATION_BLOCK,
    "guild-allow": PolicyType.DISCORD_GUILD_ALLOW,
    "guild-block": PolicyType.DISCORD_GUILD_BLOCK,
    "super-admin": PolicyType.BRIDGE_SUPER_ADMIN,
}


@dataclass(slots=True)
class ManageBridgePolicyInput:
    """Carry one add/remove request and memoize all policy lookups."""

    database: Database
    policy_service: BridgePolicyService
    ban_service: UserBanService
    discord_user_id: str
    discord_guild_id: int | None
    action: str
    policy_type_value: str
    subject: str
    reason: str | None = None
    _snapshot: BridgePolicySnapshot | None = field(default=None, init=False, repr=False)
    _normalized_subject: str | None = field(default=None, init=False, repr=False)
    _subject_error: Exception | None = field(default=None, init=False, repr=False)
    _subject_loaded: bool = field(default=False, init=False, repr=False)
    _existing: object | None = field(default=None, init=False, repr=False)
    _existing_loaded: bool = field(default=False, init=False, repr=False)

    @property
    def normalized_action(self) -> str:
        """Return canonical action text."""
        return self.action.strip().lower()

    @property
    def policy_type(self) -> PolicyType | None:
        """Return the selected policy type or ``None`` for invalid input."""
        return _TYPE_ALIASES.get(self.policy_type_value.strip().lower())

    @property
    def normalized_reason(self) -> str | None:
        """Return trimmed private reason text."""
        value = (self.reason or "").strip()
        return value or None

    def get_policy_snapshot(self) -> BridgePolicySnapshot:
        """Resolve one effective snapshot for all operation preconditions."""
        if self._snapshot is None:
            self._snapshot = self.policy_service.snapshot()
        return self._snapshot

    def get_global_ban_decision(self) -> BanDecision:
        """Return the caller's current global-ban decision."""
        return self.ban_service.check_global_discord_user(self.discord_user_id)

    def get_normalized_subject(self) -> str | None:
        """Normalize the submitted subject exactly once."""
        if not self._subject_loaded:
            try:
                if self.policy_type is None:
                    raise ValueError("Unknown policy type.")
                self._normalized_subject = self.policy_service.normalize_subject(
                    self.policy_type, self.subject
                )
            except Exception as exc:
                self._subject_error = exc
            self._subject_loaded = True
        return self._normalized_subject

    def get_existing_entry(self) -> object | None:
        """Resolve the persisted dynamic row once after subject validation."""
        if not self._existing_loaded:
            subject = self.get_normalized_subject()
            if subject is None or self.policy_type is None:
                self._existing = None
            else:
                self._existing = self.database.bridge_policy_entries.get_by_type_and_subject(
                    policy_type=self.policy_type.value,
                    normalized_subject=subject,
                )
            self._existing_loaded = True
        return self._existing


@dataclass(frozen=True, slots=True)
class ManageBridgePolicyResult:
    """Report one private management outcome."""

    applied: bool
    message: str
    reason: str


def _is_effective_super_admin(value: ManageBridgePolicyInput) -> bool:
    """Admit only effective bootstrap or dynamic super-admins."""
    return value.get_policy_snapshot().is_super_admin(value.discord_user_id)


def _is_action_valid(value: ManageBridgePolicyInput) -> bool:
    """Accept only add and remove mutations."""
    return value.normalized_action in {"add", "remove"}


def _is_policy_type_valid(value: ManageBridgePolicyInput) -> bool:
    """Accept only supported public policy type names."""
    return value.policy_type is not None


def _is_subject_valid(value: ManageBridgePolicyInput) -> bool:
    """Require a canonical hostname or decimal Discord ID."""
    return value.get_normalized_subject() is not None


def _is_reason_valid(value: ManageBridgePolicyInput) -> bool:
    """Keep stored and audit reason fields within the Discord contract."""
    return len(value.reason or "") <= 500


def _is_target_mutable(value: ManageBridgePolicyInput) -> bool:
    """Reject add/remove attempts whose effective subject is bootstrap-backed."""
    policy_type = value.policy_type
    subject = value.get_normalized_subject()
    if policy_type is None or subject is None:
        return False
    bootstrap = {
        entry.subject
        for entry in value.get_policy_snapshot().list_effective_entries(policy_type)
        if entry.source == "bootstrap"
    }
    return subject not in bootstrap


def _is_state_transition_valid(value: ManageBridgePolicyInput) -> bool:
    """Require inactive/missing state for add and active dynamic state for remove."""
    existing = value.get_existing_entry()
    if value.normalized_action == "add":
        return existing is None or getattr(existing, "status", None) == "inactive"
    return existing is not None and getattr(existing, "status", None) == "active"


class ManageBridgePolicyOperation(Operation):
    """Run ordered authorization, validation, mutation, and audit behavior."""

    name = "manage_bridge_policy"
    preconditions = (
        GLOBAL_USER_NOT_BANNED,
        GUILD_POLICY_ALLOWS_COMMAND,
        Precondition(
            name="not_effective_super_admin",
            message="Only a bridge super-admin can manage bridge policy.",
            predicate=_is_effective_super_admin,
        ),
        Precondition(
            name="invalid_action",
            message="Action must be add or remove.",
            predicate=_is_action_valid,
        ),
        Precondition(
            name="invalid_policy_type",
            message="Unknown bridge policy type.",
            predicate=_is_policy_type_valid,
        ),
        Precondition(
            name="invalid_subject",
            message="The policy subject is invalid.",
            predicate=_is_subject_valid,
        ),
        Precondition(
            name="reason_too_long",
            message="Reason must be at most 500 characters.",
            predicate=_is_reason_valid,
        ),
        Precondition(
            name="bootstrap_entry_immutable",
            message="Bootstrap policy entries cannot be changed through Discord.",
            predicate=_is_target_mutable,
        ),
        Precondition(
            name="invalid_policy_state",
            message="The dynamic policy entry is already in the requested state.",
            predicate=_is_state_transition_valid,
        ),
    )

    def reject(
        self,
        operation_input: ManageBridgePolicyInput,
        *,
        reason: str,
        message: str,
        **_: object,
    ) -> ManageBridgePolicyResult:
        """Return private rejection and audit only authorization failure."""
        if reason == "not_effective_super_admin":
            operation_input.database.management_audit.bridge_policy_manage_forbidden(
                actor_discord_user_id=operation_input.discord_user_id
            )
        return ManageBridgePolicyResult(False, message, reason)

    def body(self, operation_input: ManageBridgePolicyInput) -> ManageBridgePolicyResult:
        """Commit one valid transition with its audit row."""
        policy_type = operation_input.policy_type
        subject = operation_input.get_normalized_subject()
        assert policy_type is not None and subject is not None
        existing = operation_input.get_existing_entry()
        if operation_input.normalized_action == "add":
            activation = (
                operation_input.database.management_actions.add_or_reactivate_bridge_policy_entry(
                    actor_discord_user_id=operation_input.discord_user_id,
                    policy_type=policy_type.value,
                    normalized_subject=subject,
                    reason=operation_input.normalized_reason,
                    existing_entry_id=getattr(existing, "id", None),
                )
            )
            verb = "Added" if activation.kind == "created" else "Reactivated"
            return ManageBridgePolicyResult(
                True,
                f"{verb} `{subject}` in `{operation_input.policy_type_value}`.",
                activation.kind,
            )
        operation_input.database.management_actions.remove_bridge_policy_entry(
            actor_discord_user_id=operation_input.discord_user_id,
            entry_id=int(getattr(existing, "id")),
            removal_reason=operation_input.normalized_reason,
        )
        return ManageBridgePolicyResult(
            True,
            f"Removed `{subject}` from `{operation_input.policy_type_value}`.",
            "removed",
        )


def manage_bridge_policy_operation(value: ManageBridgePolicyInput) -> ManageBridgePolicyResult:
    """Execute one bridge-policy mutation through DiscordOps."""
    return ManageBridgePolicyOperation().execute(value)
