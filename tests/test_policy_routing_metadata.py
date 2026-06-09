"""Stage 5 scenarios for fail-closed Discord fanout routing metadata."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

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


class _Forum:
    def __init__(self, channel_id: int, calls: list[int]) -> None:
        self.channel_id = channel_id
        self.calls = calls

    async def create_thread(self, **_: object) -> tuple[object, object]:
        self.calls.append(self.channel_id)
        return SimpleNamespace(id=self.channel_id + 1000), SimpleNamespace(id=self.channel_id + 2000)


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
    def __init__(self, rows: dict[int, object], failing: set[int] | None = None) -> None:
        self.rows = rows
        self.failing = failing or set()

    def get_subscription_by_channel(self, channel_id: int) -> object | None:
        if channel_id in self.failing:
            raise RuntimeError("repository unavailable")
        return self.rows.get(channel_id)


@pytest.mark.asyncio
async def test_remote_thread_fanout_isolates_missing_and_malformed_targets(caplog: pytest.LogCaptureFixture) -> None:
    bot = _RemoteBot()
    database = SimpleNamespace(
        remote_subscriptions=_RemoteSubscriptions(
            {
                20: SimpleNamespace(discord_guild_id=None),
                30: SimpleNamespace(discord_guild_id=300),
                40: SimpleNamespace(discord_guild_id=400),
            },
            failing={10},
        )
    )
    fanout = DiscordFanout(
        bot=bot,
        mutation_tracker=_Tracker(),
        database=database,
        policy_service=_Policy(denied={400}),
    )

    results = await fanout.mirror_thread_to_siblings(
        source_thread=SimpleNamespace(id=1, name="topic"),
        source_starter_message=SimpleNamespace(content="body", author=SimpleNamespace(display_name="author")),
        sibling_channel_ids=[10, 20, 25, 30, 40],
    )

    assert [result.channel_id for result in results] == [30]
    assert bot.calls == [30]
    assert "Failed to validate Discord fanout routing metadata channel_id=10" in caplog.text
    assert "missing subscription channel_id=25" in caplog.text
    assert "invalid guild metadata channel_id=20" in caplog.text


@pytest.mark.parametrize("guild_id", [None, 0, -1, True, "123"])
def test_remote_target_rejects_invalid_guild_ids(guild_id: object) -> None:
    database = SimpleNamespace(
        remote_subscriptions=_RemoteSubscriptions({1: SimpleNamespace(discord_guild_id=guild_id)})
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
        remote_subscriptions=_RemoteSubscriptions({1: SimpleNamespace(discord_guild_id=100)})
    )
    fanout = DiscordFanout(
        bot=_RemoteBot(),
        mutation_tracker=_Tracker(),
        database=database,
        policy_service=_Policy(fail=True),
    )

    assert fanout._channel_is_allowed(1) is False


class _LocalSubscribers:
    def __init__(self, rows: list[object], by_channel: dict[int, object] | None = None) -> None:
        self.rows = rows
        self.by_channel = by_channel or {}

    def list_local_subscribers(self, _: int) -> list[object]:
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
async def test_local_thread_fanout_skips_invalid_target_and_continues_healthy_target() -> None:
    bot = _RemoteBot()
    surfaces = _LocalSurfaces()
    database = SimpleNamespace(
        local_subscribers=_LocalSubscribers(
            [
                SimpleNamespace(id=1, status="active", discord_channel_id=20, discord_guild_id=None),
                SimpleNamespace(id=2, status="active", discord_channel_id=30, discord_guild_id=300),
            ]
        ),
        local_communities=_LocalCommunities(),
        local_community_surfaces=surfaces,
    )
    fanout = LocalCommunityDiscordFanout(database=database, bot=bot, policy_service=_Policy())

    summary = await fanout.fanout_thread_to_local_subscribers(
        local_community=SimpleNamespace(id=7, discord_forum_channel_id=10, discord_guild_id=100),
        thread_row=SimpleNamespace(id=8),
        title="topic",
        content="body",
        author_display_name="author",
        source_forum_channel_id=10,
    )

    assert summary.attempted == 1
    assert summary.delivered == 1
    assert bot.calls == [30]
    assert surfaces.created == [30]


def test_local_surface_missing_or_malformed_metadata_is_fail_closed() -> None:
    database = SimpleNamespace(
        local_subscribers=_LocalSubscribers(
            [],
            by_channel={20: SimpleNamespace(discord_guild_id=0)},
        ),
        local_communities=_LocalCommunities(),
    )
    fanout = LocalCommunityDiscordFanout(database=database, bot=object(), policy_service=_Policy())

    assert fanout._surface_is_allowed(20) is False
    assert fanout._surface_is_allowed(30) is False


def test_local_surface_repository_failure_is_fail_closed() -> None:
    class _FailingSubscribers:
        def get_local_subscriber_by_channel(self, _: int) -> object:
            raise RuntimeError("repository unavailable")

    database = SimpleNamespace(
        local_subscribers=_FailingSubscribers(),
        local_communities=_LocalCommunities(),
    )
    fanout = LocalCommunityDiscordFanout(database=database, bot=object(), policy_service=_Policy())

    assert fanout._surface_is_allowed(20) is False


@pytest.mark.asyncio
async def test_local_subscriber_listing_failure_propagates_before_discord_side_effects() -> None:
    class _FailingSubscribers:
        def list_local_subscribers(self, _: int) -> list[object]:
            raise RuntimeError("repository unavailable")

    bot = _RemoteBot()
    database = SimpleNamespace(
        local_subscribers=_FailingSubscribers(),
        local_communities=_LocalCommunities(),
        local_community_surfaces=_LocalSurfaces(),
    )
    fanout = LocalCommunityDiscordFanout(database=database, bot=bot, policy_service=_Policy())

    with pytest.raises(RuntimeError, match="repository unavailable"):
        await fanout.fanout_thread_to_local_subscribers(
            local_community=SimpleNamespace(id=7, discord_forum_channel_id=10, discord_guild_id=100),
            thread_row=SimpleNamespace(id=8),
            title="topic",
            content="body",
            author_display_name="author",
            source_forum_channel_id=10,
        )

    assert bot.calls == []
