"""DiscordOps operation for private effective bridge-policy listing."""

from __future__ import annotations

from dataclasses import dataclass, field

from discordops import Operation, Precondition

from ..bridge_policy import BridgePolicyService, BridgePolicySnapshot, PolicyType
from ..user_bans import BanDecision, UserBanService
from .common_preconditions import (
    GLOBAL_USER_NOT_BANNED,
    GUILD_POLICY_ALLOWS_COMMAND,
)
from .manage_bridge_policy import _TYPE_ALIASES


@dataclass(slots=True)
class ListBridgePolicyInput:
    """Carry one private list request."""

    policy_service: BridgePolicyService
    ban_service: UserBanService
    discord_user_id: str
    discord_guild_id: int | None
    policy_type_value: str
    _snapshot: BridgePolicySnapshot | None = field(default=None, init=False, repr=False)

    @property
    def policy_type(self) -> PolicyType | None:
        """Return the selected policy type."""
        return _TYPE_ALIASES.get(self.policy_type_value.strip().lower())

    def get_policy_snapshot(self) -> BridgePolicySnapshot:
        """Return one memoized effective snapshot for this read action."""
        if self._snapshot is None:
            self._snapshot = self.policy_service.snapshot()
        return self._snapshot

    def get_global_ban_decision(self) -> BanDecision:
        """Return the caller's current global-ban decision."""
        return self.ban_service.check_global_discord_user(self.discord_user_id)


@dataclass(frozen=True, slots=True)
class ListBridgePolicyResult:
    """Return private list text or one policy rejection."""

    allowed: bool
    message: str
    reason: str


def _is_effective_super_admin(value: ListBridgePolicyInput) -> bool:
    """Admit only effective bridge super-admins."""
    return value.get_policy_snapshot().is_super_admin(value.discord_user_id)


def _is_policy_type_valid(value: ListBridgePolicyInput) -> bool:
    """Require one supported public policy type."""
    return value.policy_type is not None


class ListBridgePolicyOperation(Operation):
    """Authorize and render one effective policy type."""

    name = "list_bridge_policy"
    preconditions = (
        GLOBAL_USER_NOT_BANNED,
        GUILD_POLICY_ALLOWS_COMMAND,
        Precondition(
            name="not_effective_super_admin",
            message="Only a bridge super-admin can list bridge policy.",
            predicate=_is_effective_super_admin,
        ),
        Precondition(
            name="invalid_policy_type",
            message="Unknown bridge policy type.",
            predicate=_is_policy_type_valid,
        ),
    )

    def reject(
        self,
        operation_input: ListBridgePolicyInput,
        *,
        reason: str,
        message: str,
        **_: object,
    ) -> ListBridgePolicyResult:
        """Return one private rejection without exposing policy contents."""
        return ListBridgePolicyResult(False, message, reason)

    def perform(self, operation_input: ListBridgePolicyInput) -> ListBridgePolicyResult:
        policy_type = operation_input.policy_type
        assert policy_type is not None
        entries = operation_input.get_policy_snapshot().list_effective_entries(policy_type)
        if not entries:
            return ListBridgePolicyResult(
                True,
                f"No effective `{operation_input.policy_type_value}` entries.",
                "listed",
            )
        lines = [f"- `{entry.subject}` ({entry.source})" for entry in entries]
        return ListBridgePolicyResult(True, "\n".join(lines), "listed")


def list_bridge_policy_operation(value: ListBridgePolicyInput) -> ListBridgePolicyResult:
    """Execute one private bridge-policy list request."""
    return ListBridgePolicyOperation().execute(value)
