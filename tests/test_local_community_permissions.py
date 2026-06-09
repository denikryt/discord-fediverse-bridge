"""Unit coverage for local-community management permission policy."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.bridge_policy import BridgePolicySnapshot, EffectivePolicyEntry, PolicyType
from src.local_community_permissions import can_manage_local_community


def _policy_snapshot(*, super_admins: list[str] | None = None) -> BridgePolicySnapshot:
    """Build effective super-admin policy for permission scenarios."""
    return BridgePolicySnapshot(
        tuple(
            EffectivePolicyEntry(PolicyType.BRIDGE_SUPER_ADMIN, user_id, "bootstrap")
            for user_id in (super_admins or [])
        )
    )


def _community(owner_id: str | None) -> SimpleNamespace:
    """Build a minimal local-community object with an owner field."""
    return SimpleNamespace(created_by_discord_user_id=owner_id)


def test_permission_helper_rejects_removed_settings_keyword() -> None:
    """Permission helpers expose only the effective snapshot contract."""
    with pytest.raises(TypeError, match="settings"):
        can_manage_local_community(  # type: ignore[call-arg]
            settings=SimpleNamespace(bridge_super_admin_user_ids=[]),
            discord_user_id="111",
            local_community=_community("111"),
        )


def test_owner_is_allowed_without_super_admin_status() -> None:
    """The stored creator may manage their own local community."""
    assert can_manage_local_community(
        policy_snapshot=_policy_snapshot(super_admins=[]),
        discord_user_id="111",
        local_community=_community("111"),
    ) is True


def test_super_admin_is_allowed_without_being_owner() -> None:
    """The configured super-admin override may manage any community."""
    assert can_manage_local_community(
        policy_snapshot=_policy_snapshot(super_admins=["999"]),
        discord_user_id="999",
        local_community=_community("111"),
    ) is True


def test_non_owner_is_rejected() -> None:
    """An unrelated user must not manage an owned community."""
    assert can_manage_local_community(
        policy_snapshot=_policy_snapshot(super_admins=[]),
        discord_user_id="222",
        local_community=_community("111"),
    ) is False


def test_null_owner_is_allowed_only_for_super_admin() -> None:
    """Legacy NULL-owned communities remain protected by super-admin policy."""
    assert can_manage_local_community(
        policy_snapshot=_policy_snapshot(super_admins=[]),
        discord_user_id="111",
        local_community=_community(None),
    ) is False
    assert can_manage_local_community(
        policy_snapshot=_policy_snapshot(super_admins=["999"]),
        discord_user_id="999",
        local_community=_community(None),
    ) is True


def test_owner_id_comparison_is_string_exact() -> None:
    """Numeric-looking Discord ids are compared as opaque strings."""
    assert can_manage_local_community(
        policy_snapshot=_policy_snapshot(super_admins=[]),
        discord_user_id="0123",
        local_community=_community("123"),
    ) is False
