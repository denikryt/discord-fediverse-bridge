"""Test harness for local-community federation relay behavior.

The harness owns real SQLite persistence and the real relay fanout boundary while
keeping gateway outcomes deterministic and observable for resilience and model
exploration tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock

from src.activitypub_models import ActivityPubEvent
from src.content_publish_service import ContentPublishService
from src.fedify_gateway_client import (
    SendLocalCommunityRelayOutcome,
    SendLocalCommunityRelayResult,
)
from src.local_communities.runtime import LocalCommunityRuntime
from src.local_communities.service import LocalCommunityService
from support.db import build_database
from support.runtime import build_test_policy_service


@dataclass(frozen=True, slots=True)
class PlannedOutcome:
    """Describe one deterministic gateway result for a target actor."""

    ok: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ObservedDelivery:
    """Expose durable per-target relay state without ORM implementation details."""

    target_actor_id: str
    status: str
    attempt_count: int
    relay_activity_id: str | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class RelayObservedState:
    """Describe source, delivery, and transport effects after a relay action."""

    source_count: int
    deliveries: tuple[ObservedDelivery, ...]
    gateway_calls: tuple[tuple[str, ...], ...]


class DeterministicRelayGateway:
    """Return explicitly planned outcomes and record every target batch."""

    def __init__(self) -> None:
        """Initialize with no queued outcomes and no recorded calls."""
        self._plans: list[dict[str, PlannedOutcome]] = []
        self.calls: list[tuple[str, ...]] = []

    def plan_next(self, outcomes: dict[str, PlannedOutcome]) -> None:
        """Queue one per-target outcome plan for the next gateway call."""
        self._plans.append(dict(outcomes))

    async def send_local_community_relay(
        self,
        *,
        signing_actor_url: str,
        deliveries: list[object],
    ) -> SendLocalCommunityRelayResult:
        """Apply the next plan to the requested delivery rows."""
        del signing_actor_url
        actors = tuple(delivery.target_remote_actor_id for delivery in deliveries)
        self.calls.append(actors)
        plan = self._plans.pop(0) if self._plans else {}
        outcomes = []
        for delivery in deliveries:
            planned = plan.get(
                delivery.target_remote_actor_id,
                PlannedOutcome(ok=True),
            )
            outcomes.append(
                SendLocalCommunityRelayOutcome(
                    delivery_id=delivery.delivery_id,
                    ok=planned.ok,
                    target_remote_actor_id=delivery.target_remote_actor_id,
                    activity_id=delivery.activity_json["id"],
                    error=planned.error,
                )
            )
        return SendLocalCommunityRelayResult(outcomes=outcomes)


class LocalCommunityRelayHarness:
    """Build and observe one real local-community relay test environment."""

    def __init__(self, tmp_path: Path, *, database_name: str = "relay-harness.db") -> None:
        """Create real persistence, policy, runtime, and deterministic gateway boundaries."""
        self.database = build_database(tmp_path, database_name)
        self.gateway = DeterministicRelayGateway()
        gateway_boundary = AsyncMock()
        gateway_boundary.send_local_community_relay.side_effect = (
            self.gateway.send_local_community_relay
        )
        policy_service = build_test_policy_service(self.database)
        publish_service = ContentPublishService(
            database=self.database,
            fedify_gateway=gateway_boundary,
            bridge_prefix="[bridge]",
            bridge_policy_service=policy_service,
        )
        self.runtime = LocalCommunityRuntime(
            database=self.database,
            fedify_gateway=gateway_boundary,
            content_publish_service=publish_service,
            bridge_prefix="[bridge]",
            bridge_policy_service=policy_service,
        )
        self.gateway_boundary = gateway_boundary
        self.local_community = self._create_local_community()

    def _create_local_community(self) -> object:
        """Create the single local community used by relay scenarios."""
        LocalCommunityService(
            database=self.database,
            base_url="https://bridge.example",
            keypair_generator=lambda: ("public-key", "private-key"),
        ).create_local_community(
            discord_guild_id=10,
            discord_forum_channel_id=100,
            slug="hackers",
            name="Hackers",
            description="A local hackerspace forum.",
            created_by_discord_user_id="123",
        )
        return self.database.local_communities.get_local_community_by_slug("hackers")

    def add_subscriber(self, name: str, *, host: str = "lemmy.example") -> str:
        """Create one accepted remote subscriber and return its actor ID."""
        actor_id = f"https://{host}/u/{name}"
        self.database.remote_subscribers.create_remote_subscriber(
            local_community_id=self.local_community.id,
            remote_actor_id=actor_id,
            remote_inbox_url=f"{actor_id}/inbox",
            follow_activity_id=f"https://{host}/activities/follow/{name}",
        )
        return actor_id

    def add_default_subscribers(self) -> None:
        """Create the origin actor plus the two standard relay targets."""
        for name in ("bob", "alice", "carol"):
            self.add_subscriber(name)


    def ensure_subscriber(self, name: str, *, host: str) -> str:
        """Ensure one accepted subscriber exists without duplicating actor rows."""
        actor_id = f"https://{host}/u/{name}"
        existing = self.database.remote_subscribers.list_remote_subscribers(
            self.local_community.id, status="accepted"
        )
        if any(row.remote_actor_id == actor_id for row in existing):
            return actor_id
        return self.add_subscriber(name, host=host)

    def set_host_blocked(self, host: str, *, blocked: bool) -> None:
        """Activate or deactivate one federation block entry for target discovery."""
        repository = self.database.bridge_policy_entries
        row = repository.get_by_type_and_subject(
            policy_type="federation_block",
            normalized_subject=host,
        )
        if blocked:
            if row is None:
                repository.create_active(
                    policy_type="federation_block",
                    normalized_subject=host,
                    actor_discord_user_id="123",
                    reason="model exploration",
                )
            elif row.status != "active":
                with self.database.session() as session:
                    repository.reactivate_in_session(
                        session,
                        entry_id=row.id,
                        actor_discord_user_id="123",
                        reason="model exploration",
                    )
            return
        if row is not None and row.status == "active":
            with self.database.session() as session:
                repository.deactivate_in_session(
                    session,
                    entry_id=row.id,
                    actor_discord_user_id="123",
                )

    def remove_subscriber(self, actor_id: str) -> None:
        """Remove one accepted subscriber from the current target set."""
        self.database.remote_subscribers.delete_remote_subscriber(
            local_community_id=self.local_community.id,
            remote_actor_id=actor_id,
        )

    def post_event(self, *, suffix: str = "1", source_json: bool = True) -> ActivityPubEvent:
        """Build one inbound post event with a stable relay identity."""
        source = {
            "type": "Create",
            "id": f"https://lemmy.example/activities/create/post/{suffix}",
            "actor": "https://lemmy.example/u/bob",
            "object": {
                "type": "Page",
                "id": f"https://lemmy.example/post/{suffix}",
                "attributedTo": "https://lemmy.example/u/bob",
                "name": "Remote topic",
                "content": "<p>hello</p>",
            },
        }
        return ActivityPubEvent.model_validate(
            {
                "event_type": "post.created",
                "delivery_id": source["id"],
                "source_activity_json": source if source_json else None,
                "source_activity_id": source["id"],
                "source_announce_id": "https://lemmy.example/activities/announce/1",
                "occurred_at": "2026-05-19T10:00:00Z",
                "community_actor_id": "https://bridge.example/communities/hackers",
                "actor_id": "https://lemmy.example/u/bob",
                "object": {
                    "ap_id": f"https://lemmy.example/post/{suffix}",
                    "kind": "post",
                    "lemmy_id": 1,
                    "post_ap_id": None,
                    "post_lemmy_id": None,
                    "parent_ap_id": None,
                    "title": "Remote topic",
                    "body_markdown": "hello",
                    "url": f"https://lemmy.example/post/{suffix}",
                    "published_at": "2026-05-19T10:00:00Z",
                    "author_name": "bob",
                },
            }
        )

    def observe(self, event: ActivityPubEvent, *, operation: str = "create") -> RelayObservedState:
        """Collect durable relay state and recorded gateway target batches."""
        source = self.database.local_community_relay.get_local_community_relay_source_activity(
            local_community_id=self.local_community.id,
            operation=operation,
            source_object_ap_id=event.object.ap_id,
            source_activity_id=event.source_activity_id,
        )
        if source is None:
            deliveries: tuple[ObservedDelivery, ...] = ()
            source_count = 0
        else:
            rows = self.database.local_community_relay.list_local_community_relay_deliveries_for_source(
                source.id
            )
            deliveries = tuple(
                ObservedDelivery(
                    target_actor_id=row.target_remote_actor_id,
                    status=row.status,
                    attempt_count=row.attempt_count,
                    relay_activity_id=row.relay_activity_id,
                    last_error=row.last_error,
                )
                for row in sorted(rows, key=lambda item: item.target_remote_actor_id)
            )
            source_count = 1
        return RelayObservedState(
            source_count=source_count,
            deliveries=deliveries,
            gateway_calls=tuple(self.gateway.calls),
        )
