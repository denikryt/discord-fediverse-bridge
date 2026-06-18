"""Behavior scenarios for the extracted raw Discord mutation handler."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.discord_message_mutations import DiscordMessageMutationHandler


class _Clock:
    """Deterministic clock used to test volatile echo suppression windows."""

    def __init__(self, now: float) -> None:
        """Store the current test time as seconds since an arbitrary epoch."""
        self.now = now

    def __call__(self) -> float:
        """Return the currently configured test time."""
        return self.now


class _DeliveryRepo:
    """Record remote delivery lookups and return a configured delivery row."""

    def __init__(self, delivery: object | None = None) -> None:
        """Initialise the fake with an optional remote-community delivery."""
        self.delivery = delivery
        self.lookups: list[int] = []

    def get_message_delivery_by_message(self, message_id: int) -> object | None:
        """Return the configured delivery while recording the lookup boundary."""
        self.lookups.append(message_id)
        return self.delivery


class _MappingRepo:
    """Return one optional personal-feed mapping for admission tests."""

    def __init__(self, mapping: object | None = None) -> None:
        """Store the mapping returned for all Discord message ids."""
        self.mapping = mapping
        self.lookups: list[int] = []

    def get_message_mapping_by_discord_message_id(self, message_id: int) -> object | None:
        """Return the configured mapping and record the queried Discord id."""
        self.lookups.append(message_id)
        return self.mapping


class _ContentMutationRepo:
    """Record durable reconciliation requests issued by partial edit failures."""

    def __init__(self) -> None:
        """Create an empty request log."""
        self.requests: list[dict[str, object]] = []

    def request_discord_reconciliation(self, **kwargs: object) -> None:
        """Record the durable reconciliation request shape."""
        self.requests.append(kwargs)


class _Database:
    """Minimal database facade used by raw mutation handler scenarios."""

    def __init__(
        self,
        *,
        delivery: object | None = None,
        mapping: object | None = None,
    ) -> None:
        """Expose only repositories used at the raw Discord boundary."""
        self.discord_fanout_groups = _DeliveryRepo(delivery)
        self.message_mappings = _MappingRepo(mapping)
        self.content_mutations = _ContentMutationRepo()


class _Router:
    """Fake DiscordEventRouter preserving the methods used by the handler."""

    def __init__(self, *, local: bool = False) -> None:
        """Configure whether the tested message is a local-community surface."""
        self.local = local
        self.local_checks: list[int] = []
        self.handle_message_edit = AsyncMock()
        self.handle_message_delete = AsyncMock()

    def is_local_community_message(self, message_id: int) -> bool:
        """Return the configured local-community classification."""
        self.local_checks.append(message_id)
        return self.local


class _Fetcher:
    """Fake Discord fetch boundary for partial raw edit reconciliation."""

    def __init__(self, channel: object | None = None) -> None:
        """Store the channel returned from the cache-first fetch path."""
        self.channel = channel
        self.get_calls: list[int] = []
        self.fetch_channel = AsyncMock(return_value=channel)

    def get_channel(self, channel_id: int) -> object | None:
        """Return the cached channel and record the lookup."""
        self.get_calls.append(channel_id)
        return self.channel


def _edit_payload(
    *,
    message_id: int = 11,
    channel_id: int = 10,
    content: str = "edited",
    edited_timestamp: str | None = "2026-06-17T12:00:00Z",
) -> object:
    """Build a raw edit payload with the fields used by the handler."""
    data: dict[str, object] = {"content": content, "author": {"username": "alice"}}
    if edited_timestamp is not None:
        data["edited_timestamp"] = edited_timestamp
    return SimpleNamespace(message_id=message_id, channel_id=channel_id, guild_id=1, data=data)


def _delete_payload(*, message_id: int = 11, channel_id: int = 10) -> object:
    """Build a raw delete payload with the fields used by the handler."""
    return SimpleNamespace(message_id=message_id, channel_id=channel_id, guild_id=1)


def _handler(
    *,
    database: _Database,
    router: _Router,
    fetcher: _Fetcher | None = None,
    runtime: object | None = None,
    clock: _Clock | None = None,
) -> DiscordMessageMutationHandler:
    """Create the handler with deterministic outer-boundary collaborators."""
    return DiscordMessageMutationHandler(
        database=database,
        event_router=router,
        discord_fetcher=fetcher or _Fetcher(),
        runtime_provider=lambda: runtime,
        clock=clock or _Clock(100.0),
    )


@pytest.mark.asyncio
async def test_echo_edit_suppression_expires_before_remote_routing() -> None:
    """A bridge-originated edit is skipped only inside the volatile echo window."""
    clock = _Clock(100.0)
    database = _Database(delivery=SimpleNamespace(role="source"))
    router = _Router()
    handler = _handler(database=database, router=router, runtime=SimpleNamespace(), clock=clock)

    handler.track_message_edit(11)
    clock.now = 104.0
    await handler.handle_raw_message_edit(_edit_payload())

    assert database.discord_fanout_groups.lookups == []
    router.handle_message_edit.assert_not_awaited()

    clock.now = 106.0
    await handler.handle_raw_message_edit(_edit_payload(content="after echo"))

    assert database.discord_fanout_groups.lookups == [11]
    router.handle_message_edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_echo_delete_suppression_expires_before_remote_routing() -> None:
    """A bridge-originated delete is skipped only inside the volatile echo window."""
    clock = _Clock(100.0)
    database = _Database(delivery=SimpleNamespace(role="source"))
    router = _Router()
    handler = _handler(database=database, router=router, runtime=SimpleNamespace(), clock=clock)

    handler.track_message_delete(11)
    clock.now = 104.0
    await handler.handle_raw_message_delete(_delete_payload())

    assert database.discord_fanout_groups.lookups == []
    router.handle_message_delete.assert_not_awaited()

    clock.now = 106.0
    await handler.handle_raw_message_delete(_delete_payload())

    assert database.discord_fanout_groups.lookups == [11]
    router.handle_message_delete.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["mirror", "inbound"])
async def test_remote_edit_role_guard_skips_non_source_deliveries(role: str) -> None:
    """Remote-community raw edits are admitted only from source deliveries."""
    database = _Database(delivery=SimpleNamespace(role=role))
    router = _Router()
    handler = _handler(database=database, router=router, runtime=SimpleNamespace())

    await handler.handle_raw_message_edit(_edit_payload())

    router.handle_message_edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_delete_inbound_guard_skips_delivery() -> None:
    """Inbound AP mirror deletes remain owned by ActivityPub handling, not Discord raw events."""
    database = _Database(delivery=SimpleNamespace(role="inbound"))
    router = _Router()
    handler = _handler(database=database, router=router, runtime=SimpleNamespace())

    await handler.handle_raw_message_delete(_delete_payload())

    router.handle_message_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_edit_fetch_success_uses_authoritative_message_state() -> None:
    """A partial raw edit is reconciled through Discord before router admission."""
    edited_at = datetime(2026, 6, 17, 12, 30, tzinfo=timezone.utc)
    channel = SimpleNamespace(
        fetch_message=AsyncMock(
            return_value=SimpleNamespace(edited_at=edited_at, content="authoritative")
        )
    )
    database = _Database(delivery=SimpleNamespace(role="source"))
    router = _Router()
    handler = _handler(
        database=database,
        router=router,
        fetcher=_Fetcher(channel),
        runtime=SimpleNamespace(),
    )

    await handler.handle_raw_message_edit(_edit_payload(edited_timestamp=None))

    router.handle_message_edit.assert_awaited_once()
    kwargs = router.handle_message_edit.await_args.kwargs
    assert kwargs["new_content"] == "authoritative"
    assert kwargs["edited_timestamp"] is edited_at


@pytest.mark.asyncio
async def test_partial_edit_fetch_failure_records_reconciliation_and_wakes_worker(monkeypatch) -> None:
    """A Discord fetch failure is converted into existing durable reconciliation work."""

    class FakeHTTPException(Exception):
        """Stand in for discord.HTTPException at the platform boundary."""

    monkeypatch.setattr("src.discord_message_mutations.discord.HTTPException", FakeHTTPException)
    channel = SimpleNamespace(fetch_message=AsyncMock(side_effect=FakeHTTPException("temporary")))
    database = _Database(delivery=SimpleNamespace(role="source"))
    router = _Router()
    wake = Mock()
    handler = _handler(
        database=database,
        router=router,
        fetcher=_Fetcher(channel),
        runtime=SimpleNamespace(user_actor_delivery_worker=SimpleNamespace(wake=wake)),
    )

    await handler.handle_raw_message_edit(_edit_payload(edited_timestamp=None))

    assert database.content_mutations.requests == [
        {"channel_id": 10, "message_id": 11, "error": "temporary"}
    ]
    wake.assert_called_once()
    router.handle_message_edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_personal_feed_admission_bypasses_remote_delivery_lookup() -> None:
    """Personal-feed mappings are admitted without requiring remote fanout delivery rows."""
    database = _Database(mapping=SimpleNamespace(publication_context="personal_feed"))
    router = _Router()
    handler = _handler(database=database, router=router, runtime=SimpleNamespace())

    await handler.handle_raw_message_edit(_edit_payload())
    await handler.handle_raw_message_delete(_delete_payload())

    assert database.discord_fanout_groups.lookups == []
    router.handle_message_edit.assert_awaited_once()
    router.handle_message_delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_local_community_admission_bypasses_remote_delivery_lookup() -> None:
    """Local-community surfaces are admitted without remote-community fanout rows."""
    database = _Database()
    router = _Router(local=True)
    handler = _handler(database=database, router=router, runtime=SimpleNamespace())

    await handler.handle_raw_message_edit(_edit_payload())
    await handler.handle_raw_message_delete(_delete_payload())

    assert database.discord_fanout_groups.lookups == []
    router.handle_message_edit.assert_awaited_once()
    router.handle_message_delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_absence_skips_admitted_edit_and_delete() -> None:
    """Admitted raw events still return when Stage 4 runtime wiring is unavailable."""
    database = _Database(delivery=SimpleNamespace(role="source"))
    router = _Router()
    handler = _handler(database=database, router=router, runtime=None)

    await handler.handle_raw_message_edit(_edit_payload())
    await handler.handle_raw_message_delete(_delete_payload())

    router.handle_message_edit.assert_not_awaited()
    router.handle_message_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_router_exceptions_do_not_escape_raw_callback_boundary() -> None:
    """Router failures are contained so Discord callbacks do not crash the client."""
    database = _Database(delivery=SimpleNamespace(role="source"))
    router = _Router()
    router.handle_message_edit.side_effect = RuntimeError("edit boom")
    router.handle_message_delete.side_effect = RuntimeError("delete boom")
    handler = _handler(database=database, router=router, runtime=SimpleNamespace())

    await handler.handle_raw_message_edit(_edit_payload())
    await handler.handle_raw_message_delete(_delete_payload())

    router.handle_message_edit.assert_awaited_once()
    router.handle_message_delete.assert_awaited_once()
