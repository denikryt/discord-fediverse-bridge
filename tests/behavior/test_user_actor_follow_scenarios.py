"""Behavior scenarios for registered User Actor follower lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.activitypub_handlers import dispatch_activitypub_event
from src.activitypub_models import LocalFollowRequestEvent, LocalUnfollowRequestEvent
from src.db import Database
from src.user_actor_followers import UserActorFollowService


class _Gateway:
    """Fake only the signed gateway delivery boundary."""

    def __init__(self) -> None:
        self.accepts: list[dict[str, str]] = []
        self.fail_next = False

    async def accept_user_actor_follow(self, **payload: str) -> None:
        """Record a signed-Accept request or simulate one transient failure."""
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("accept failed")
        self.accepts.append(payload)


class _DiscordUser:
    """Capture owner-visible direct messages at the Discord boundary."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        """Record one attempted owner notification."""
        self.messages.append(message)


class _Bot:
    """Resolve the one registered owner used by each scenario."""

    def __init__(self) -> None:
        self.user = _DiscordUser()

    async def fetch_user(self, _user_id: int) -> _DiscordUser:
        """Return the stable fake Discord owner."""
        return self.user


@pytest.fixture
def follow_runtime(tmp_path):
    """Build a real SQLite follower service behind the normal dispatch path."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    database.migrate()
    user = database.users.create_user(
        discord_user_id="42",
        activitypub_username="alice",
        actor_url="https://bridge.example/actors/alice",
        inbox_url="https://bridge.example/actors/alice/inbox",
        outbox_url="https://bridge.example/actors/alice/outbox",
        followers_url="https://bridge.example/actors/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    gateway = _Gateway()
    bot = _Bot()
    service = UserActorFollowService(
        database=database,
        fedify_gateway=gateway,
        bot=bot,
    )
    runtime = SimpleNamespace(
        database=database,
        user_actor_follow_service=service,
        bridge_policy_service=SimpleNamespace(
            federation_decision=lambda _subject: SimpleNamespace(
                allowed=True, reason=SimpleNamespace(value="allowed")
            )
        ),
    )
    yield runtime, user, gateway, bot
    database.close()


def _follow(
    *, follow_id: str, actor_id: str = "https://remote.example/users/bob"
) -> LocalFollowRequestEvent:
    """Build one strict internal event matching gateway normalization."""
    return LocalFollowRequestEvent(
        event_type="local.follow_requested",
        delivery_id=follow_id,
        occurred_at=datetime.now(timezone.utc),
        target_actor_kind="user",
        target_actor_id="https://bridge.example/actors/alice",
        actor_id=actor_id,
        object={
            "follow_activity_id": follow_id,
            "remote_inbox_url": f"{actor_id}/inbox",
            "remote_shared_inbox_url": "https://remote.example/inbox",
        },
    )


@pytest.mark.asyncio
async def test_follow_duplicate_refreshes_one_relationship_and_notifies_once(
    follow_runtime,
):
    """A duplicate Follow re-sends Accept without duplicating relationship or DM."""
    runtime, user, gateway, bot = follow_runtime

    first = await dispatch_activitypub_event(
        _follow(follow_id="https://remote.example/follows/1"), runtime
    )
    second = await dispatch_activitypub_event(
        _follow(follow_id="https://remote.example/follows/2"), runtime
    )

    row = runtime.database.user_actor_followers.get_relationship(
        user_id=user.id, remote_actor_id="https://remote.example/users/bob"
    )
    assert first.status == second.status == "processed"
    assert row is not None and row.follow_activity_id.endswith("/2")
    assert row.owner_notification_state == "attempted"
    assert len(gateway.accepts) == 2
    assert len(bot.user.messages) == 1


@pytest.mark.asyncio
async def test_failed_accept_keeps_notification_pending_until_duplicate_succeeds(
    follow_runtime,
):
    """A duplicate Follow recovers Accept and notification after a transient failure."""
    runtime, user, gateway, bot = follow_runtime
    gateway.fail_next = True

    with pytest.raises(RuntimeError, match="accept failed"):
        await dispatch_activitypub_event(
            _follow(follow_id="https://remote.example/follows/1"), runtime
        )
    pending = runtime.database.user_actor_followers.get_relationship(
        user_id=user.id, remote_actor_id="https://remote.example/users/bob"
    )
    assert pending is not None and pending.owner_notification_state == "pending"

    await dispatch_activitypub_event(
        _follow(follow_id="https://remote.example/follows/1"), runtime
    )
    recovered = runtime.database.user_actor_followers.get_relationship(
        user_id=user.id, remote_actor_id="https://remote.example/users/bob"
    )
    assert recovered is not None and recovered.id == pending.id
    assert recovered.owner_notification_state == "attempted"
    assert len(bot.user.messages) == 1


@pytest.mark.asyncio
async def test_historical_follow_id_cannot_be_reused_by_another_actor(follow_runtime):
    """Refresh never releases a prior Follow ID for hostile reuse."""
    runtime, user, gateway, _bot = follow_runtime
    f1 = "https://remote.example/follows/1"
    await dispatch_activitypub_event(_follow(follow_id=f1), runtime)
    await dispatch_activitypub_event(
        _follow(follow_id="https://remote.example/follows/2"), runtime
    )

    collision = await dispatch_activitypub_event(
        _follow(follow_id=f1, actor_id="https://remote.example/users/mallory"), runtime
    )

    assert collision.status == "skipped"
    assert (
        runtime.database.user_actor_followers.get_relationship(
            user_id=user.id, remote_actor_id="https://remote.example/users/mallory"
        )
        is None
    )
    assert len(gateway.accepts) == 2


@pytest.mark.asyncio
async def test_undo_and_refollow_create_a_new_relationship_incarnation(follow_runtime):
    """Undo deletes the current row while a later Follow earns a fresh notification."""
    runtime, user, _gateway, bot = follow_runtime
    await dispatch_activitypub_event(
        _follow(follow_id="https://remote.example/follows/1"), runtime
    )
    first = runtime.database.user_actor_followers.get_relationship(
        user_id=user.id, remote_actor_id="https://remote.example/users/bob"
    )
    undo = LocalUnfollowRequestEvent(
        event_type="local.unfollow_requested",
        delivery_id="https://remote.example/undo/1",
        occurred_at=datetime.now(timezone.utc),
        target_actor_kind="user",
        target_actor_id=user.actor_url,
        actor_id="https://remote.example/users/bob",
        object={"follow_activity_id": "https://remote.example/follows/1"},
    )
    await dispatch_activitypub_event(undo, runtime)
    await dispatch_activitypub_event(
        _follow(follow_id="https://remote.example/follows/2"), runtime
    )
    second = runtime.database.user_actor_followers.get_relationship(
        user_id=user.id, remote_actor_id="https://remote.example/users/bob"
    )

    assert first is not None and second is not None and second.id != first.id
    assert len(bot.user.messages) == 2
