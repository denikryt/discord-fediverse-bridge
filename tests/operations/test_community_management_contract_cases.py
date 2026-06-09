"""Executable typed contracts for local-community management."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.bridge_policy import BridgePolicyService
from src.local_communities.service import LocalCommunityService
from src.operations import CreateCommunityInput, EditCommunityInput
from src.operations.create_community import create_community_operation
from src.operations.edit_community import edit_community_operation
from support.community_management_contracts import (
    COMMUNITY_MANAGEMENT_CASES,
    CommunityManagementCase,
)
from support.community_management_effects import (
    CommunityManagementObserved,
    collect_community_management_effects,
)
from support.db import build_database


def _settings(*, super_admin: bool = False) -> SimpleNamespace:
    """Build settings consumed by management operations."""

    return SimpleNamespace(
        bridge_super_admin_user_ids=["999"] if super_admin else [],
        normalized_fedify_origin="https://bridge.example",
    )


def _seed_community(database: object, *, status: str) -> None:
    """Create the operation target and optionally change lifecycle state."""

    LocalCommunityService(
        database=database,
        base_url="https://bridge.example",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=100,
        slug="cats",
        name="Cats",
        description="Old summary",
        created_by_discord_user_id="111",
    )
    if status == "disabled":
        community = database.local_communities.get_local_community_by_slug("cats")
        with database.session() as session:
            persisted = session.merge(community)
            persisted.status = "disabled"


def _execute(
    case: CommunityManagementCase, tmp_path: Path
) -> CommunityManagementObserved:
    """Execute one case through real operations and persistence."""

    database = build_database(
        tmp_path, f"community-contract-{case.id.replace('.', '-')}.db"
    )
    if case.action == "edit" and case.community_state != "missing":
        _seed_community(database, status=case.community_state)
    audit_offset = len(database.management_audit_events.list_oldest_first())

    if case.action == "create":
        result = create_community_operation(
            CreateCommunityInput(
                database=database,
                settings=_settings(),
                discord_user_id="111",
                discord_guild_id=10,
                discord_forum_channel_id=100,
                slug=case.slug,
                name=case.display_name,
                description=case.summary,
            )
        )
    else:
        user_id = {"owner": "111", "super_admin": "999", "unauthorized": "222"}[
            case.caller_role
        ]
        guild_id = {"same": 10, "other": 20, "dm": None}[case.guild_context]
        settings = _settings(super_admin=case.caller_role == "super_admin")
        result = edit_community_operation(
            EditCommunityInput(
                database=database,
                settings=settings,
                discord_user_id=user_id,
                discord_guild_id=guild_id,
                community_slug=case.slug,
                display_name=case.display_name,
                summary=case.summary,
                status=case.requested_status,
                policy_service=BridgePolicyService(
                    settings=settings,
                    repository=database.bridge_policy_entries,
                ),
            )
        )

    return collect_community_management_effects(
        database=database,
        result=result,
        slug=case.slug,
        audit_offset=audit_offset,
    )


@pytest.mark.parametrize("case", COMMUNITY_MANAGEMENT_CASES, ids=lambda case: case.id)
def test_community_management_contract(
    case: CommunityManagementCase, tmp_path: Path
) -> None:
    """Real operation effects must match the independently declared contract."""

    observed = _execute(case, tmp_path)
    expected = case.expected
    assert observed == CommunityManagementObserved(
        applied=expected.applied,
        reason=expected.reason,
        display_name=expected.display_name,
        summary=expected.summary,
        status=expected.status,
        audit_events=expected.audit_events,
    )
