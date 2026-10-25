"""participant mutation support scenarios for fail-closed Discord fanout routing metadata."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from support.fake_platform_adapter import FakeDiscordPort
from src.community_sync.discord_fanout import DiscordFanout
from src.local_communities.discord_fanout import LocalCommunityDiscordFanout


class _Snapshot:
    def __init__(self, denied: set[int] | None = None) -> None:
        self.denied = denied or set()

    def is_discord_guild_allowed(self, guild_id: int) -> bool:
        return guild_id not in self.denied


class _Policy:
    def __init__(self, denied: set[int] | None = None, *, fail: bool = False) -> None:
        self.denied = denied
        self.fail = fail

    def snapshot(self) -> _Snapshot:
        if self.fail:
            raise RuntimeError("policy unavailable")
        return _Snapshot(self.denied)

    def is_discord_guild_allowed(self, guild_id: int) -> bool:
        return self.snapshot().is_discord_guild_allowed(guild_id)


class _Forum:
    def __init__(self, channel_id: int, calls: list[int]) -> None:
        self.channel_id = channel_id
        self.calls = calls

    async def create_thread(self, **_: object) -> tuple[object, object]:
        self.calls.append(self.channel_id)
        return SimpleNamespace(id=self.channel_id + 1000), SimpleNamespace(
            id=self.channel_id + 2000
        )


class _RemoteBot:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def fetch_forum_channel(self, channel_id: int) -> _Forum:
        return _Forum(channel_id, self.calls)


class _Tracker:
    def __init__(self) -> None:
        self.edits: list[int] = []
        self.deletes: list[int] = []

    def track_message_edit(self, message_id: int) -> None:
        self.edits.append(message_id)

    def track_message_delete(self, message_id: int) -> None:
        self.deletes.append(message_id)


class _RemoteSubscriptions:
    def __init__(
        self, rows: dict[int, object], failing: set[int] | None = None
    ) -> None:
        self.rows = rows
        self.failing = failing or set()

    def get_subscription_by_channel(self, channel_id: int) -> object | None:
        if channel_id in self.failing:
            raise RuntimeError("repository unavailable")
        return self.rows.get(channel_id)


@pytest.mark.asyncio
async def test_remote_thread_fanout_isolates_missing_and_malformed_targets(tmp_path, caplog):
    """Malformed and unavailable routing metadata cannot block a healthy sibling."""
    from test_normalized_community_runtime import world, starter
    from src.models import ChannelCommunitySubscription, ChannelSurface
    from sqlalchemy import select

    db, runtime, gateway, adapter = world(tmp_path, forum_ids=(100, 10, 20, 30, 40))
    try:
        community = db.remote_subscriptions.get_subscription_by_channel(100).lemmy_community_actor_id
        with db.immediate_session() as session:
            malformed = session.scalar(select(ChannelCommunitySubscription).where(
                ChannelCommunitySubscription.discord_channel_id == 20))
            malformed.discord_guild_id = None
        original = db.remote_subscriptions.get_subscription_by_channel
        def lookup(channel):
            """Model a failed repository read for one target only."""
            if channel == 10:
                raise RuntimeError('repository unavailable')
            return original(channel)
        db.remote_subscriptions.get_subscription_by_channel = lookup
        # Deny a separate target via its current guild metadata.
        with db.immediate_session() as session:
            sub = session.scalar(select(ChannelCommunitySubscription).where(
                ChannelCommunitySubscription.discord_channel_id == 40))
            sub.discord_guild_id = 400
            session.scalar(select(ChannelSurface).where(ChannelSurface.remote_subscription_id == sub.id)).external_guild_id = '400'
        runtime.discord_fanout.policy_service = _Policy(denied={400})
        event, view = starter()
        assert (await runtime.handle_normalized_thread_create(event, view, adapter=adapter)).status == 'published'
        assert [call[0].external_channel_id for call in adapter.posts] == ['30']
        group = db.discord_fanout_groups.get_thread_group_by_ap_object(gateway.requests[0].object_id)
        assert len(db.discord_fanout_groups.get_thread_deliveries(group.id)) == 2
        assert runtime.discord_fanout._normalized_target(25, community) is None
        assert runtime.discord_fanout._normalized_target(20, community) is None
        assert runtime.discord_fanout._normalized_target(40, community) is None
        assert 'Could not authorize community sibling forum 10' in caplog.text
    finally:
        db.close()


@pytest.mark.parametrize("guild_id", [None, 0, -1, True, "123"])
def test_remote_target_rejects_invalid_guild_ids(guild_id: object) -> None:
    database = SimpleNamespace(
        remote_subscriptions=_RemoteSubscriptions(
            {1: SimpleNamespace(discord_guild_id=guild_id)}
        )
    )
    fanout = DiscordFanout(
        bot=_RemoteBot(),
        mutation_tracker=_Tracker(),
        database=database,
        policy_service=_Policy(),
    )

    assert fanout._channel_is_allowed(1) is False


def test_remote_policy_failure_is_fail_closed() -> None:
    database = SimpleNamespace(
        remote_subscriptions=_RemoteSubscriptions(
            {1: SimpleNamespace(discord_guild_id=100)}
        )
    )
    fanout = DiscordFanout(
        bot=_RemoteBot(),
        mutation_tracker=_Tracker(),
        database=database,
        policy_service=_Policy(fail=True),
    )

    assert fanout._channel_is_allowed(1) is False


class _LocalSubscribers:
    def __init__(
        self, rows: list[object], by_channel: dict[int, object] | None = None
    ) -> None:
        self.rows = rows
        self.by_channel = by_channel or {}

    def list_discord_active_subscribers(self, _: int) -> list[object]:
        return self.rows

    def get_local_subscriber_by_channel(self, channel_id: int) -> object | None:
        return self.by_channel.get(channel_id)


class _LocalCommunities:
    def __init__(self, by_channel: dict[int, object] | None = None) -> None:
        self.by_channel = by_channel or {}

    def get_local_community_by_forum_channel_id(self, channel_id: int) -> object | None:
        return self.by_channel.get(channel_id)


class _LocalSurfaces:
    def __init__(self) -> None:
        self.created: list[int] = []

    def get_local_community_thread_surface(self, **_: object) -> None:
        return None

    def create_local_community_thread_surface(self, **kwargs: object) -> object:
        self.created.append(int(kwargs["discord_forum_channel_id"]))
        return SimpleNamespace(**kwargs)


@pytest.mark.asyncio
async def test_local_thread_fanout_skips_invalid_target_and_continues_healthy_target(tmp_path):
    """A malformed subscriber is skipped while a healthy copy is committed."""
    from test_normalized_local_community_runtime import world, starter
    from src.models import LocalSubscriber

    db, community, subscribers, runtime, gateway, adapter = world(tmp_path)
    try:
        with db.immediate_session() as session:
            session.get(LocalSubscriber, subscribers[200].id).discord_guild_id = None
        event, view = starter()
        assert (await runtime.handle_normalized_thread_create(event, view, adapter=adapter)).status == 'published'
        assert [call[0].external_channel_id for call in adapter.posts] == ['300']
        row = db.local_community_content.get_local_community_thread_by_ap_object_id(gateway.requests[0].object_id)
        assert len(db.local_community_surfaces.list_local_community_thread_surfaces(row.id)) == 2
    finally:
        db.close()


def test_local_surface_missing_or_malformed_metadata_is_fail_closed() -> None:
    database = SimpleNamespace(
        local_subscribers=_LocalSubscribers(
            [],
            by_channel={20: SimpleNamespace(
                id=4, local_community_id=7, status="active", discord_guild_id=0
            )},
        ),
        local_communities=_LocalCommunities(),
    )
    fanout = LocalCommunityDiscordFanout(
        database=database, bot=object(), policy_service=_Policy()
    )

    # Validate persisted receipts, not the obsolete channel-ID-only API.
    assert fanout._surface_is_allowed(SimpleNamespace(
        role="local_subscriber", discord_forum_channel_id=20,
        local_subscriber_id=4,
    ), community_id=7) is False
    assert fanout._surface_is_allowed(SimpleNamespace(
        role="local_subscriber", discord_forum_channel_id=30,
        local_subscriber_id=5,
    ), community_id=7) is False


def test_local_surface_repository_failure_is_fail_closed() -> None:
    class _FailingSubscribers:
        def get_local_subscriber_by_channel(self, _: int) -> object:
            raise RuntimeError("repository unavailable")

    database = SimpleNamespace(
        local_subscribers=_FailingSubscribers(),
        local_communities=_LocalCommunities(),
    )
    fanout = LocalCommunityDiscordFanout(
        database=database, bot=object(), policy_service=_Policy()
    )

    assert fanout._surface_is_allowed(SimpleNamespace(
        role="local_subscriber", discord_forum_channel_id=20,
        local_subscriber_id=4,
    ), community_id=7) is False


@pytest.mark.asyncio
async def test_local_subscriber_listing_failure_propagates_before_discord_side_effects() -> (
    None
):
    class _FailingSubscribers:
        def list_discord_active_subscribers(self, _: int) -> list[object]:
            raise RuntimeError("repository unavailable")

    bot = _RemoteBot()
    database = SimpleNamespace(
        local_subscribers=_FailingSubscribers(),
        local_communities=_LocalCommunities(),
        local_community_surfaces=_LocalSurfaces(),
        platform_surfaces=SimpleNamespace(get_discord_host_placement=lambda _: None),
    )
    fanout = LocalCommunityDiscordFanout(
        database=database, bot=bot, policy_service=_Policy()
    )

    with pytest.raises(RuntimeError, match="repository unavailable"):
        await fanout.fanout_normalized_thread(
            adapter=FakeDiscordPort(),
            include_host=False,
            local_community=SimpleNamespace(
                id=7, discord_forum_channel_id=10, discord_guild_id=100
            ),
            thread_row=SimpleNamespace(id=8, local_community_id=7),
            title="topic",
            content="body",
            author_display_name="author",
            source_forum_channel_id=10,
        )

    assert bot.calls == []
