"""Execute typed bridge-policy contracts through real service and operation paths."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.bridge_policy import BridgePolicyService, PolicyType
from src.operations import ManageBridgePolicyInput, manage_bridge_policy_operation
from src.user_bans import UserBanService
from support.bridge_policy_contracts import (
    BRIDGE_POLICY_CONTRACT_CASES,
    BridgePolicyContractCase,
)
from support.bridge_policy_effects import (
    assert_bridge_policy_effects,
    collect_bridge_policy_effects,
)
from support.db import build_database


_POLICY_ALIASES = {
    "federation-allow": PolicyType.FEDERATION_ALLOW,
    "federation-block": PolicyType.FEDERATION_BLOCK,
    "guild-allow": PolicyType.DISCORD_GUILD_ALLOW,
    "guild-block": PolicyType.DISCORD_GUILD_BLOCK,
    "super-admin": PolicyType.BRIDGE_SUPER_ADMIN,
}


def _settings(case: BridgePolicyContractCase) -> SimpleNamespace:
    """Build the effective bootstrap policy declared by one case."""

    federation_allow = case.bootstrap_allow if case.action == "federation_decision" or case.policy_type.startswith("federation") else ()
    federation_block = case.bootstrap_block if case.action == "federation_decision" or case.policy_type.startswith("federation") else ()
    guild_allow = case.bootstrap_allow if case.action == "guild_decision" or case.policy_type.startswith("guild") else ()
    guild_block = case.bootstrap_block if case.action == "guild_decision" or case.policy_type.startswith("guild") else ()
    if case.guild_context == "blocked":
        guild_block = tuple(sorted(set(guild_block) | {"200"}))
    return SimpleNamespace(
        federation_allowlist=list(federation_allow),
        federation_blocklist=list(federation_block),
        discord_guild_allowlist=list(guild_allow),
        discord_guild_blocklist=list(guild_block),
        bridge_super_admin_user_ids=["100"] if case.caller_role == "super_admin" else [],
    )


def _seed_dynamic_state(database: object, case: BridgePolicyContractCase) -> None:
    """Persist the requested active/inactive row before the action."""

    if case.existing_dynamic_state == "absent":
        return
    policy_type = _POLICY_ALIASES[case.policy_type]
    subject = BridgePolicyService.normalize_subject(policy_type, case.subject)
    row = database.bridge_policy_entries.create_active(
        policy_type=policy_type.value,
        normalized_subject=subject,
        actor_discord_user_id="100",
        reason="existing",
    )
    if case.existing_dynamic_state == "inactive":
        with database.session() as session:
            persisted = session.merge(row)
            persisted.status = "inactive"


@pytest.mark.parametrize(
    "case",
    BRIDGE_POLICY_CONTRACT_CASES,
    ids=lambda case: case.id,
)
def test_bridge_policy_contract(case: BridgePolicyContractCase, tmp_path: Path) -> None:
    """Run one declared bridge-policy contract against real persistence."""

    database = build_database(tmp_path, f"{case.id}.db")
    settings = _settings(case)
    _seed_dynamic_state(database, case)
    service = BridgePolicyService(
        settings=settings,
        repository=database.bridge_policy_entries,
    )
    audit_offset = len(database.management_audit_events.list_oldest_first())

    if case.action == "federation_decision":
        if case.existing_dynamic_state == "active":
            policy_type = _POLICY_ALIASES[case.policy_type]
            normalized = BridgePolicyService.normalize_subject(policy_type, case.subject)
            if database.bridge_policy_entries.get_by_type_and_subject(
                policy_type=policy_type.value,
                normalized_subject=normalized,
            ) is None:
                database.bridge_policy_entries.create_active(
                    policy_type=policy_type.value,
                    normalized_subject=normalized,
                    actor_discord_user_id="100",
                    reason="dynamic",
                )
        decision = service.federation_decision(case.subject)
        observed = collect_bridge_policy_effects(
            database=database,
            reason="evaluated",
            decision_allowed=decision.allowed,
            decision_reason=decision.reason.value,
            audit_offset=audit_offset,
        )
    elif case.action == "guild_decision":
        allowed = service.is_discord_guild_allowed(case.subject)
        observed = collect_bridge_policy_effects(
            database=database,
            reason="evaluated",
            decision_allowed=allowed,
            decision_reason="allowed" if allowed else "guild_not_allowed",
            audit_offset=audit_offset,
        )
    else:
        result = manage_bridge_policy_operation(
            ManageBridgePolicyInput(
                database=database,
                policy_service=service,
                ban_service=UserBanService(database=database, settings=None),
                discord_user_id="100" if case.caller_role == "super_admin" else "999",
                discord_guild_id=None if case.guild_context == "dm" else 200,
                action=case.action,
                policy_type_value=case.policy_type,
                subject=case.subject,
                reason="contract reason",
            )
        )
        observed = collect_bridge_policy_effects(
            database=database,
            reason=result.reason,
            applied=result.applied,
            audit_offset=audit_offset,
        )

    assert_bridge_policy_effects(observed, case.expected)
