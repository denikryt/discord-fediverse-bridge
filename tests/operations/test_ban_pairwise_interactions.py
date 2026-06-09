"""Execute constrained pairwise ban interactions through real operations."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.bridge_policy import BridgePolicyService
from src.local_communities.service import LocalCommunityService
from src.operations import BanUserInput, UnbanUserInput, ban_user_operation, unban_user_operation
from support.ban_effects import assert_ban_effects, collect_ban_effects
from support.ban_interactions import BAN_INTERACTION_CASES, BanInteractionCase
from support.db import build_database


def _settings(case: BanInteractionCase) -> SimpleNamespace:
    """Build explicit identity and super-admin settings."""

    return SimpleNamespace(
        bridge_super_admin_user_ids=["999"] if case.caller_role == "super_admin" else [],
        public_base_url="https://bridge.example",
        normalized_public_base_url="https://bridge.example",
    )


def _seed_community(database: object, case: BanInteractionCase) -> object | None:
    """Create the constrained community state when it can exist."""

    if case.scope == "global" or case.community_state == "missing":
        return None
    LocalCommunityService(
        database=database,
        base_url="https://bridge.example",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=100,
        slug="cats",
        name="Cats",
        description="Cats community.",
        created_by_discord_user_id="111",
    )
    community = database.local_communities.get_local_community_by_slug("cats")
    if case.community_state == "disabled":
        with database.session() as session:
            persisted = session.merge(community)
            persisted.status = "disabled"
        community = database.local_communities.get_local_community_by_slug("cats")
    return community


def _seed_target(database: object, case: BanInteractionCase) -> str:
    """Return a valid remote handle or persist the local identity."""

    if case.target_kind == "remote":
        return "alice@example.com"
    database.users.create_user(
        discord_user_id="777",
        activitypub_username="alice",
        actor_url="https://bridge.example/users/alice",
        inbox_url="https://bridge.example/users/alice/inbox",
        outbox_url="https://bridge.example/users/alice/outbox",
        followers_url="https://bridge.example/users/alice/followers",
        public_key_pem="public-key",
        private_key_pem="private-key",
    )
    return "alice@bridge.example"


def _seed_existing(
    database: object,
    case: BanInteractionCase,
    community: object | None,
    actor_handle: str,
) -> None:
    """Create the valid prior lifecycle state declared by the interaction."""

    if case.existing_ban_state == "absent":
        return
    activation = database.management_actions.create_or_reactivate_ban(
        actor_discord_user_id="111",
        local_community_id=None if case.scope == "global" else community.id,
        actor_handle=actor_handle,
        actor_url=None,
        target_discord_user_id="777" if case.target_kind == "local" else None,
        reason="existing",
    )
    if case.existing_ban_state == "removed":
        database.management_actions.remove_ban(
            actor_discord_user_id="111",
            local_community_id=None if case.scope == "global" else community.id,
            actor_handle=actor_handle,
        )
    assert activation.ban.id is not None


@pytest.mark.parametrize("case", BAN_INTERACTION_CASES, ids=lambda case: case.id)
def test_ban_pairwise_interaction(case: BanInteractionCase, tmp_path: Path) -> None:
    """Compare each selected real operation with the independent reference table."""

    database = build_database(tmp_path, f"pairwise-{case.id}.db")
    community = _seed_community(database, case)
    actor_handle = _seed_target(database, case)
    _seed_existing(database, case, community, actor_handle)
    audit_offset = len(database.management_audit_events.list_oldest_first())
    settings = _settings(case)
    policy_service = BridgePolicyService(
        settings=settings,
        repository=database.bridge_policy_entries,
    )
    caller_id = {"owner": "111", "super_admin": "999", "unauthorized": "222"}[
        case.caller_role
    ]
    community_slug = None if case.scope == "global" else "cats"
    guild_id = None if case.scope == "global" else 10

    if case.action == "ban":
        result = ban_user_operation(
            BanUserInput(
                database=database,
                settings=settings,
                discord_user_id=caller_id,
                discord_guild_id=guild_id,
                community_slug=community_slug,
                actor_handle=actor_handle,
                reason="pairwise reason",
                policy_service=policy_service,
            )
        )
        target_discord_user_id = result.target_discord_user_id
    else:
        result = unban_user_operation(
            UnbanUserInput(
                database=database,
                settings=settings,
                discord_user_id=caller_id,
                discord_guild_id=guild_id,
                community_slug=community_slug,
                actor_handle=actor_handle,
                policy_service=policy_service,
            )
        )
        target_discord_user_id = None

    observed = collect_ban_effects(
        database=database,
        result=result,
        target_discord_user_id=target_discord_user_id,
        audit_offset=audit_offset,
    )
    assert_ban_effects(observed, case.expected)
