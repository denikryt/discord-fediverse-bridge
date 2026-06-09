"""Structural checks for concrete DiscordOps policy operation contracts."""

from __future__ import annotations

from src.operations.ban_user import BanUserOperation
from src.operations.edit_community import EditCommunityOperation
from src.operations.list_banned_users import ListBannedUsersOperation
from src.operations.list_bridge_policy import ListBridgePolicyOperation
from src.operations.manage_bridge_policy import ManageBridgePolicyOperation
from src.operations.unban_user import UnbanUserOperation


def test_policy_operations_implement_body_and_reject_without_perform_adapter() -> None:
    """Concrete operations implement the framework contract directly."""
    for operation_type in (
        BanUserOperation,
        EditCommunityOperation,
        ListBannedUsersOperation,
        ListBridgePolicyOperation,
        ManageBridgePolicyOperation,
        UnbanUserOperation,
    ):
        assert "body" in operation_type.__dict__
        assert "reject" in operation_type.__dict__
        assert "perform" not in operation_type.__dict__
