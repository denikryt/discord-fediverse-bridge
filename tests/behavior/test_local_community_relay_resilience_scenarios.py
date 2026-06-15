"""Resilience scenarios for durable local-community federation relay."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.bridge_policy import PolicyType
from support.local_community_relay import (
    LocalCommunityRelayHarness,
    PlannedOutcome,
)


@pytest.mark.asyncio
async def test_relay_policy_read_failure_happens_before_persistence_or_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed policy read leaves no durable or external relay effects."""
    harness = LocalCommunityRelayHarness(tmp_path)
    harness.add_default_subscribers()
    event = harness.post_event(suffix="policy-failure")

    def fail_snapshot() -> object:
        """Simulate repository failure at the first policy boundary."""
        raise RuntimeError("policy read failed")

    monkeypatch.setattr(
        harness.runtime.federation_fanout.policy_service,
        "snapshot",
        fail_snapshot,
    )

    with pytest.raises(RuntimeError, match="policy read failed"):
        await harness.runtime.federation_fanout.relay_create(
            event=event,
            local_community=harness.local_community,
            object_kind="post",
        )

    assert harness.observe(event).source_count == 0
    harness.gateway_boundary.send_local_community_relay.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_relay_failure_retries_only_failed_target(tmp_path: Path) -> None:
    """A failed target remains retryable without redelivering healthy targets."""
    harness = LocalCommunityRelayHarness(tmp_path)
    harness.add_default_subscribers()
    event = harness.post_event(suffix="partial-retry")
    alice = "https://lemmy.example/u/alice"
    carol = "https://lemmy.example/u/carol"
    harness.gateway.plan_next(
        {
            alice: PlannedOutcome(ok=True),
            carol: PlannedOutcome(ok=False, error="temporary failure"),
        }
    )
    harness.gateway.plan_next({carol: PlannedOutcome(ok=True)})

    first = await harness.runtime.federation_fanout.relay_create(
        event=event,
        local_community=harness.local_community,
        object_kind="post",
    )
    second = await harness.runtime.federation_fanout.relay_create(
        event=event,
        local_community=harness.local_community,
        object_kind="post",
    )

    assert (first.attempted, first.delivered, first.failed) == (2, 1, 1)
    assert (second.attempted, second.delivered, second.failed) == (1, 1, 0)
    observed = harness.observe(event)
    assert observed.gateway_calls == ((alice, carol), (carol,))
    assert {
        row.target_actor_id: (row.status, row.attempt_count)
        for row in observed.deliveries
    } == {
        alice: ("delivered", 1),
        carol: ("delivered", 2),
    }


@pytest.mark.asyncio
async def test_policy_change_during_relay_applies_to_next_action_only(
    tmp_path: Path,
) -> None:
    """An in-flight relay keeps its snapshot while the next action sees policy changes."""
    harness = LocalCommunityRelayHarness(tmp_path)
    harness.add_default_subscribers()
    entered_gateway = asyncio.Event()
    release_gateway = asyncio.Event()
    original_send = harness.gateway.send_local_community_relay

    async def blocking_gateway(**kwargs: object):
        """Pause after selection so policy changes between two action boundaries."""
        entered_gateway.set()
        await release_gateway.wait()
        return await original_send(**kwargs)

    harness.gateway_boundary.send_local_community_relay.side_effect = blocking_gateway
    first_event = harness.post_event(suffix="snapshot-a")
    first_task = asyncio.create_task(
        harness.runtime.federation_fanout.relay_create(
            event=first_event,
            local_community=harness.local_community,
            object_kind="post",
        )
    )
    await entered_gateway.wait()
    harness.database.bridge_policy_entries.create_active(
        policy_type=PolicyType.FEDERATION_BLOCK.value,
        normalized_subject="lemmy.example",
        actor_discord_user_id="123",
        reason="maintenance",
    )
    release_gateway.set()
    first = await first_task

    harness.gateway_boundary.send_local_community_relay.reset_mock(side_effect=True)
    harness.gateway_boundary.send_local_community_relay.side_effect = (
        harness.gateway.send_local_community_relay
    )
    second_event = harness.post_event(suffix="snapshot-b")
    second = await harness.runtime.federation_fanout.relay_create(
        event=second_event,
        local_community=harness.local_community,
        object_kind="post",
    )

    assert (first.attempted, first.delivered, first.failed) == (2, 2, 0)
    assert second.attempted == 0
    assert len(harness.gateway.calls) == 1


@pytest.mark.asyncio
async def test_failed_delivery_is_not_retried_while_target_is_ineligible(
    tmp_path: Path,
) -> None:
    """Retry durable failures only while the target is currently eligible."""
    harness = LocalCommunityRelayHarness(tmp_path)
    harness.add_subscriber("bob", host="lemmy.example")
    alice = harness.add_subscriber("alice", host="alice.example")
    event = harness.post_event(suffix="eligibility-retry")
    harness.gateway.plan_next(
        {alice: PlannedOutcome(ok=False, error="temporary failure")}
    )

    first = await harness.runtime.federation_fanout.relay_create(
        event=event,
        local_community=harness.local_community,
        object_kind="post",
    )
    harness.remove_subscriber(alice)
    second = await harness.runtime.federation_fanout.relay_create(
        event=event,
        local_community=harness.local_community,
        object_kind="post",
    )

    observed_while_removed = harness.observe(event)
    row = next(
        delivery
        for delivery in observed_while_removed.deliveries
        if delivery.target_actor_id == alice
    )
    assert (first.attempted, first.failed) == (1, 1)
    assert second.attempted == 0
    assert row.status == "failed"
    assert row.attempt_count == 1
    assert observed_while_removed.gateway_calls == ((alice,),)

    harness.ensure_subscriber("alice", host="alice.example")
    harness.gateway.plan_next({alice: PlannedOutcome(ok=True)})
    third = await harness.runtime.federation_fanout.relay_create(
        event=event,
        local_community=harness.local_community,
        object_kind="post",
    )

    observed_after_reaccept = harness.observe(event)
    row = next(
        delivery
        for delivery in observed_after_reaccept.deliveries
        if delivery.target_actor_id == alice
    )
    assert (third.attempted, third.delivered) == (1, 1)
    assert row.status == "delivered"
    assert row.attempt_count == 2
    assert observed_after_reaccept.gateway_calls == ((alice,), (alice,))
