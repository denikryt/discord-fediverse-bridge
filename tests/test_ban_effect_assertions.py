"""Verify diagnostic behavior of the ban-specific effect assertion helper."""

from __future__ import annotations

import pytest

from support.ban_contracts import BanExpected
from support.ban_effects import BanObservedEffects, assert_ban_effects


def test_effect_assertion_identifies_audit_mismatch() -> None:
    """Expose the mismatched effect category instead of opaque object inequality."""

    observed = BanObservedEffects(
        applied=False,
        reason="cannot_manage_community",
        rows=(),
        audit_events=(),
        target_discord_user_id=None,
    )
    expected = BanExpected(
        applied=False,
        reason="cannot_manage_community",
        active_rows=0,
        audit_events=(("ban.create_forbidden", "forbidden"),),
    )

    with pytest.raises(AssertionError, match="management audit effects"):
        assert_ban_effects(observed, expected)
