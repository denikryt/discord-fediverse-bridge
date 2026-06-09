"""Cross-entry-point and metamorphic assurance for Discord guild policy."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from discordops import evaluate_policy

from src.bridge_policy import BridgePolicyService
from src.operations.common_preconditions import CommandAccessInput, GUILD_COMMAND_ACCESS
from src.user_bans import BanDecision


@dataclass(frozen=True, slots=True)
class GuildPolicyCase:
    """Declare one independent guild access expectation."""

    id: str
    guild_id: int
    allow: tuple[str, ...]
    block: tuple[str, ...]
    expected_allowed: bool


CASES = (
    GuildPolicyCase("empty_open", 200, (), (), True),
    GuildPolicyCase("listed_allowed", 200, ("200",), (), True),
    GuildPolicyCase("unlisted_denied", 200, ("201",), (), False),
    GuildPolicyCase("blocked_denied", 200, (), ("200",), False),
    GuildPolicyCase("block_overrides_allow", 200, ("200",), ("200",), False),
)


class _Repository:
    """Return explicit dynamic rows for the real policy service."""

    def __init__(self, rows: tuple[object, ...] = ()) -> None:
        self.rows = rows

    def list_all_active(self) -> list[object]:
        return list(self.rows)


class _BanService:
    def check_global_discord_user(self, discord_user_id: str) -> BanDecision:
        return BanDecision(False)


def _service(case: GuildPolicyCase, *, unrelated: bool = False) -> BridgePolicyService:
    rows: tuple[object, ...] = ()
    if unrelated:
        rows = (
            SimpleNamespace(
                policy_type="discord_guild_block",
                normalized_subject="999",
            ),
        )
    settings = SimpleNamespace(
        federation_allowlist=[],
        federation_blocklist=[],
        discord_guild_allowlist=list(case.allow),
        discord_guild_blocklist=list(case.block),
        bridge_super_admin_user_ids=[],
    )
    return BridgePolicyService(settings=settings, repository=_Repository(rows))


def _direct_adapter(case: GuildPolicyCase, *, unrelated: bool = False) -> bool:
    return _service(case, unrelated=unrelated).is_discord_guild_allowed(case.guild_id)


def _command_policy_adapter(case: GuildPolicyCase, *, unrelated: bool = False) -> bool:
    service = _service(case, unrelated=unrelated)
    result = evaluate_policy(
        GUILD_COMMAND_ACCESS,
        CommandAccessInput(
            settings=service.settings,
            database=None,
            discord_guild_id=case.guild_id,
            discord_user_id="123",
            policy_service=service,
            ban_service=_BanService(),
        ),
    )
    return result.allowed


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_guild_policy_matches_explicit_contract_through_each_entry_point(
    case: GuildPolicyCase,
) -> None:
    """Each path must independently match the declared expected result."""

    assert _direct_adapter(case) is case.expected_allowed
    assert _command_policy_adapter(case) is case.expected_allowed


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_unrelated_policy_entries_do_not_change_target_guild_result(
    case: GuildPolicyCase,
) -> None:
    """Adding a distinct blocked guild preserves the target contract in both paths."""

    assert _direct_adapter(case, unrelated=True) is case.expected_allowed
    assert _command_policy_adapter(case, unrelated=True) is case.expected_allowed
