"""Defined failure-boundary exploration for local-community relay."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given, settings
from hypothesis.strategies import sampled_from

import src.local_communities.federation_fanout as fanout_module
from support.local_community_relay import LocalCommunityRelayHarness

ALICE = "https://lemmy.example/u/alice"
CAROL = "https://lemmy.example/u/carol"


@pytest.mark.asyncio
async def test_renderer_failure_marks_claimed_target_safe_failed_without_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rendering fails before network, so only the CAS-claimed target is retryable."""
    harness = LocalCommunityRelayHarness(tmp_path, database_name="renderer-failure.db")
    harness.add_default_subscribers()
    event = harness.post_event(suffix="renderer-failure")

    def fail_render(**_: object) -> dict[str, object]:
        raise RuntimeError("render failed")

    monkeypatch.setattr(
        fanout_module,
        "render_local_community_relay_activity",
        fail_render,
    )

    with pytest.raises(RuntimeError, match="render failed"):
        await harness.runtime.federation_fanout.relay_create(
            event=event,
            local_community=harness.local_community,
            object_kind="post",
        )

    observed = harness.observe(event)
    assert observed.source_count == 1
    assert {
        row.target_actor_id: (row.status, row.attempt_count)
        for row in observed.deliveries
    } == {
        ALICE: ("failed", 1),
        CAROL: ("pending", 0),
    }
    harness.gateway_boundary.send_local_community_relay.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_exception_remains_unknown_and_never_blind_retries(tmp_path: Path) -> None:
    """A gateway exception after claims may follow remote send and is unresolved."""
    harness = LocalCommunityRelayHarness(tmp_path, database_name="gateway-exception.db")
    harness.add_default_subscribers()
    event = harness.post_event(suffix="gateway-exception")
    harness.gateway_boundary.send_local_community_relay.side_effect = RuntimeError(
        "gateway unavailable"
    )

    first = await harness.runtime.federation_fanout.relay_create(
        event=event, local_community=harness.local_community, object_kind="post",
    )
    second = await harness.runtime.federation_fanout.relay_create(
        event=event, local_community=harness.local_community, object_kind="post",
    )
    assert (first.attempted, first.failed, second.attempted) == (2, 2, 0)

    observed = harness.observe(event)
    assert observed.source_count == 1
    assert all(row.status == "unknown" for row in observed.deliveries)
    assert all(row.attempt_count == 1 for row in observed.deliveries)


@pytest.mark.asyncio
async def test_second_outcome_persistence_failure_keeps_first_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistence failure keeps its attempted target fenced as sending."""
    harness = LocalCommunityRelayHarness(tmp_path, database_name="result-failure.db")
    harness.add_default_subscribers()
    event = harness.post_event(suffix="result-failure")
    repository = harness.database.local_community_relay
    original_mark = repository.finish_local_community_relay_target
    call_count = 0

    def fail_second_result(**kwargs: object):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("result persistence failed")
        return original_mark(**kwargs)

    monkeypatch.setattr(
        repository,
        "finish_local_community_relay_target",
        fail_second_result,
    )

    with pytest.raises(RuntimeError, match="result persistence failed"):
        await harness.runtime.federation_fanout.relay_create(
            event=event,
            local_community=harness.local_community,
            object_kind="post",
        )

    rows = sorted(
        harness.observe(event).deliveries, key=lambda row: row.target_actor_id
    )
    assert [(row.status, row.attempt_count) for row in rows] == [
        ("delivered", 1),
        ("sending", 1),
    ]


@given(failure_boundary=sampled_from(("renderer", "gateway")))
@settings(max_examples=4, deadline=None)
def test_defined_pre_result_failures_are_classified_by_network_boundary(
    failure_boundary: str,
) -> None:
    """Only render failures are proven unsent; gateway failure is unresolved."""
    import asyncio

    with TemporaryDirectory() as directory:
        harness = LocalCommunityRelayHarness(
            Path(directory),
            database_name=f"generated-{failure_boundary}.db",
        )
        harness.add_default_subscribers()
        event = harness.post_event(suffix=f"generated-{failure_boundary}")
        patcher = pytest.MonkeyPatch()
        try:
            if failure_boundary == "renderer":
                patcher.setattr(
                    fanout_module,
                    "render_local_community_relay_activity",
                    lambda **_: (_ for _ in ()).throw(RuntimeError("render failed")),
                )
            else:
                harness.gateway_boundary.send_local_community_relay.side_effect = (
                    RuntimeError("gateway unavailable")
                )

            if failure_boundary == "renderer":
                with pytest.raises(RuntimeError, match="render failed"):
                    asyncio.run(harness.runtime.federation_fanout.relay_create(
                        event=event, local_community=harness.local_community, object_kind="post",
                    ))
            else:
                asyncio.run(harness.runtime.federation_fanout.relay_create(
                    event=event, local_community=harness.local_community, object_kind="post",
                ))

            observed = harness.observe(event)
            assert observed.source_count == 1
            if failure_boundary == "renderer":
                assert {row.status for row in observed.deliveries} == {"failed", "pending"}
                assert {row.attempt_count for row in observed.deliveries} == {0, 1}
            else:
                assert {row.status for row in observed.deliveries} == {"unknown"}
                assert {row.attempt_count for row in observed.deliveries} == {1}
        finally:
            patcher.undo()
            harness.database.engine.dispose()
