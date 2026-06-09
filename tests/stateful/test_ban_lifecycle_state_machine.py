"""Stateful model testing for one community-scoped remote ban lifecycle."""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from hypothesis import HealthCheck, settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test
from sqlalchemy import select

from src.bridge_policy import BridgePolicyService
from src.local_communities.service import LocalCommunityService
from src.models import CommunityActorBan
from src.operations import BanUserInput, UnbanUserInput, ban_user_operation, unban_user_operation
from src.user_bans import UserBanService
from support.db import build_database


class ModelState(StrEnum):
    """Independent product-relevant lifecycle states."""

    ABSENT = "absent"
    ACTIVE = "active"
    REMOVED = "removed"


class BanLifecycleMachine(RuleBasedStateMachine):
    """Generate repeated create/remove sequences against real operations."""

    def __init__(self) -> None:
        """Create one isolated persistent SUT and independent model state."""

        super().__init__()
        self._temporary_directory = TemporaryDirectory()
        root = Path(self._temporary_directory.name)
        self.database = build_database(root, "stateful-ban.db")
        self.settings = SimpleNamespace(
            bridge_super_admin_user_ids=[],
            public_base_url="https://bridge.example",
            normalized_public_base_url="https://bridge.example",
        )
        LocalCommunityService(
            database=self.database,
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
        self.community = self.database.local_communities.get_local_community_by_slug(
            "cats"
        )
        assert self.community is not None
        self.policy_service = BridgePolicyService(
            settings=self.settings,
            repository=self.database.bridge_policy_entries,
        )
        self.ban_service = UserBanService(database=self.database, settings=self.settings)
        self.audit_offset = len(
            self.database.management_audit_events.list_oldest_first()
        )
        self.model_state = ModelState.ABSENT
        self.expected_audits: list[tuple[str, str]] = []

    def teardown(self) -> None:
        """Release the temporary database directory after one generated example."""

        self.database.engine.dispose()
        self._temporary_directory.cleanup()

    def _ban_input(self) -> BanUserInput:
        """Build the real scoped ban operation input."""

        return BanUserInput(
            database=self.database,
            settings=self.settings,
            discord_user_id="111",
            discord_guild_id=10,
            community_slug="cats",
            actor_handle="alice@example.com",
            reason="stateful reason",
            policy_service=self.policy_service,
        )

    def _unban_input(self) -> UnbanUserInput:
        """Build the real scoped unban operation input."""

        return UnbanUserInput(
            database=self.database,
            settings=self.settings,
            discord_user_id="111",
            discord_guild_id=10,
            community_slug="cats",
            actor_handle="alice@example.com",
            policy_service=self.policy_service,
        )

    @rule()
    def create_ban(self) -> None:
        """Apply the independent create/reactivate/duplicate transition table."""

        previous = self.model_state
        result = ban_user_operation(self._ban_input())
        if previous is ModelState.ABSENT:
            assert result.applied is True
            assert result.reason == "created"
            self.model_state = ModelState.ACTIVE
            self.expected_audits.append(("ban.created", "success"))
        elif previous is ModelState.REMOVED:
            assert result.applied is True
            assert result.reason == "reactivated"
            self.model_state = ModelState.ACTIVE
            self.expected_audits.append(("ban.reactivated", "success"))
        else:
            assert result.applied is False
            assert result.reason == "duplicate_active_ban"

    @rule()
    def remove_ban(self) -> None:
        """Apply the independent remove/no-active transition table."""

        previous = self.model_state
        result = unban_user_operation(self._unban_input())
        if previous is ModelState.ACTIVE:
            assert result.applied is True
            assert result.reason == "unbanned"
            self.model_state = ModelState.REMOVED
            self.expected_audits.append(("ban.removed", "success"))
        else:
            assert result.applied is False
            assert result.reason == "no_active_ban"

    @rule()
    def query_effective_ban(self) -> None:
        """Query enforcement without changing independent model state."""

        decision = self.ban_service.check_activitypub_actor(
            local_community_id=int(self.community.id),
            actor_url="https://remote.example/u/alice",
            actor_handle="alice@example.com",
        )
        assert decision.banned is (self.model_state is ModelState.ACTIVE)

    @invariant()
    def persistence_audit_and_enforcement_match_model(self) -> None:
        """Compare all observable lifecycle state with the independent model."""

        with self.database.session() as session:
            rows = tuple(
                session.scalars(
                    select(CommunityActorBan).order_by(CommunityActorBan.id)
                )
            )
        assert len(rows) <= 1
        if self.model_state is ModelState.ABSENT:
            assert rows == ()
        else:
            assert len(rows) == 1
            assert rows[0].actor_handle == "alice@example.com"
            expected_status = (
                "active" if self.model_state is ModelState.ACTIVE else "inactive"
            )
            assert rows[0].status == expected_status
            assert rows[0].local_community_id == self.community.id

        events = self.database.management_audit_events.list_oldest_first()[
            self.audit_offset :
        ]
        assert [(event.action, event.result) for event in events] == self.expected_audits

        decision = self.ban_service.check_activitypub_actor(
            local_community_id=int(self.community.id),
            actor_url="https://remote.example/u/alice",
            actor_handle="alice@example.com",
        )
        assert decision.banned is (self.model_state is ModelState.ACTIVE)


def test_generated_ban_lifecycle_sequences() -> None:
    """Run bounded stateful examples and retain Hypothesis shrinking."""

    ci = os.environ.get("HYPOTHESIS_PROFILE") == "ci"
    run_state_machine_as_test(
        BanLifecycleMachine,
        settings=settings(
            max_examples=50 if ci else 20,
            stateful_step_count=25 if ci else 15,
            deadline=None,
            suppress_health_check=(HealthCheck.too_slow,),
        ),
    )
