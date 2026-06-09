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
async def test_renderer_failure_keeps_pending_rows_without_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rendering fails after source and target rows are durably created."""
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
        ALICE: ("pending", 0),
        CAROL: ("pending", 0),
    }
    harness.gateway_boundary.send_local_community_relay.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_exception_keeps_pending_rows_retryable(tmp_path: Path) -> None:
    """A raised transport call leaves all attempted rows pending with zero attempts."""
    harness = LocalCommunityRelayHarness(tmp_path, database_name="gateway-exception.db")
    harness.add_default_subscribers()
    event = harness.post_event(suffix="gateway-exception")
    harness.gateway_boundary.send_local_community_relay.side_effect = RuntimeError(
        "gateway unavailable"
    )

    with pytest.raises(RuntimeError, match="gateway unavailable"):
        await harness.runtime.federation_fanout.relay_create(
            event=event,
            local_community=harness.local_community,
            object_kind="post",
        )

    observed = harness.observe(event)
    assert observed.source_count == 1
    assert all(row.status == "pending" for row in observed.deliveries)
    assert all(row.attempt_count == 0 for row in observed.deliveries)


@pytest.mark.asyncio
async def test_second_outcome_persistence_failure_keeps_first_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-outcome sessions permit earlier result commits before a later failure."""
    harness = LocalCommunityRelayHarness(tmp_path, database_name="result-failure.db")
    harness.add_default_subscribers()
    event = harness.post_event(suffix="result-failure")
    repository = harness.database.local_community_relay
    original_mark = repository.mark_local_community_relay_delivery_result
    call_count = 0

    def fail_second_result(**kwargs: object):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("result persistence failed")
        return original_mark(**kwargs)

    monkeypatch.setattr(
        repository,
        "mark_local_community_relay_delivery_result",
        fail_second_result,
    )

    with pytest.raises(RuntimeError, match="result persistence failed"):
        await harness.runtime.federation_fanout.relay_create(
            event=event,
            local_community=harness.local_community,
            object_kind="post",
        )

    rows = sorted(harness.observe(event).deliveries, key=lambda row: row.target_actor_id)
    assert [(row.status, row.attempt_count) for row in rows] == [
        ("delivered", 1),
        ("pending", 0),
    ]


@given(failure_boundary=sampled_from(("renderer", "gateway")))
@settings(max_examples=4, deadline=None)
def test_defined_pre_result_failures_preserve_retryable_rows(
    failure_boundary: str,
) -> None:
    """Both defined pre-result failures preserve pending rows for later retry."""
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
                harness.gateway_boundary.send_local_community_relay.side_effect = RuntimeError(
                    "gateway unavailable"
                )

            with pytest.raises(RuntimeError):
                asyncio.run(
                    harness.runtime.federation_fanout.relay_create(
                        event=event,
                        local_community=harness.local_community,
                        object_kind="post",
                    )
                )

            observed = harness.observe(event)
            assert observed.source_count == 1
            assert {row.status for row in observed.deliveries} == {"pending"}
            assert {row.attempt_count for row in observed.deliveries} == {0}
        finally:
            patcher.undo()
            harness.database.engine.dispose()
