"""Observable SQLite scenarios for connector identity, claims and copy integrity."""

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.schema import CreateTable

from src.db import Database
from src.models import (
    ChannelSurface,
    PlatformConnector,
    LocalCommunity,
    LocalSubscriber,
    ChannelCommunitySubscription,
    PersonalFeedBinding,
    User,
    LocalCommunityThreadSurface,
    LocalCommunityMessageSurface,
)


@pytest.fixture
def database(tmp_path):
    """Use a file-backed fresh schema with real FK-enforcing connections."""
    db = Database(f"sqlite:///{tmp_path / 'v2.db'}")
    db.migrate()
    yield db
    db.close()


def _owners(session):
    """Create valid domain rows without inventing non-Discord location IDs."""
    community = LocalCommunity(
        slug="local",
        display_name="Local",
        actor_url="https://bridge.test/actors/communities/local",
        inbox_url="https://bridge.test/actors/communities/local/inbox",
        outbox_url="https://bridge.test/actors/communities/local/outbox",
        followers_url="https://bridge.test/actors/communities/local/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    user = User(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://bridge.test/actors/users/alice",
        inbox_url="https://bridge.test/actors/users/alice/inbox",
        outbox_url="https://bridge.test/actors/users/alice/outbox",
        followers_url="https://bridge.test/actors/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    session.add_all([community, user])
    session.flush()
    subscriber = LocalSubscriber(local_community_id=community.id)
    remote = ChannelCommunitySubscription(
        lemmy_community_actor_id="https://remote.test/c/news", status="failed"
    )
    personal = PersonalFeedBinding(user_id=user.id)
    session.add_all([subscriber, remote, personal])
    session.flush()
    return {
        "local_community_id": community.id,
        "local_subscriber_id": subscriber.id,
        "remote_subscription_id": remote.id,
        "personal_feed_binding_id": personal.id,
    }


def _connector(session, key="fluxer-a"):
    """Persist a distinct connection; synthetic Fluxer has no network behavior."""
    row = PlatformConnector(
        connector_key=key, platform="fluxer", instance_origin=f"https://{key}.test"
    )
    session.add(row)
    session.flush()
    return row


def test_owners_compute_roles_and_release_keeps_location(database):
    """Claim, switch and release owners; readable role follows only typed FKs."""
    with database.session() as session:
        owners = _owners(session)
        connector = _connector(session)
        surface = ChannelSurface(
            connector_id=connector.id, external_channel_id="900", kind="forum"
        )
        session.add(surface)
        session.flush()
        assert surface.role is None
        original_id = surface.id
        for column, role in zip(
            owners,
            ["local_host", "local_subscriber", "remote_subscription", "personal_feed"],
        ):
            for owner_column in owners:
                setattr(
                    surface,
                    owner_column,
                    owners[column] if owner_column == column else None,
                )
            session.flush()
            session.refresh(surface)
            assert surface.id == original_id and surface.role == role
            assert (
                session.scalar(
                    select(ChannelSurface.role).where(ChannelSurface.role == role)
                )
                == role
            )
        surface.personal_feed_binding_id = None
        session.flush()
        session.refresh(surface)
        assert surface.id == original_id and surface.role is None


def test_two_owners_and_duplicate_owner_cannot_be_persisted(database):
    """SQLite rejects double ownership and claiming one owner on two surfaces."""
    with database.session() as session:
        owners = _owners(session)
        connector = _connector(session)
        first = ChannelSurface(
            connector_id=connector.id,
            external_channel_id="900",
            kind="forum",
            remote_subscription_id=owners["remote_subscription_id"],
        )
        session.add(first)
        session.flush()
        for values in [
            dict(
                local_subscriber_id=owners["local_subscriber_id"],
                remote_subscription_id=owners["remote_subscription_id"],
            ),
            dict(remote_subscription_id=owners["remote_subscription_id"]),
        ]:
            with pytest.raises(IntegrityError), session.begin_nested():
                session.add(
                    ChannelSurface(
                        connector_id=connector.id,
                        external_channel_id="901",
                        kind="forum",
                        **values,
                    )
                )
                session.flush()
        assert session.scalar(select(ChannelSurface.role)) == "remote_subscription"


def test_generated_role_is_not_writable_and_is_stored(database):
    """Direct writes fail, while introspection and PostgreSQL DDL preserve STORED."""
    with database.session() as session:
        connector = _connector(session)
        surface = ChannelSurface(
            connector_id=connector.id, external_channel_id="900", kind="forum"
        )
        session.add(surface)
        session.flush()
        with pytest.raises(OperationalError), session.begin_nested():
            session.execute(
                text("UPDATE platform_channel_surfaces SET role='remote_subscription'")
            )
        with pytest.raises(OperationalError), session.begin_nested():
            session.execute(
                text("INSERT INTO platform_channel_surfaces(role) VALUES('local_host')")
            )
        columns = session.execute(
            text("PRAGMA table_xinfo(platform_channel_surfaces)")
        ).all()
        assert next(row for row in columns if row[1] == "role")[6] == 3
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []
    dialect = postgresql.dialect()
    dialect.server_version_info = (12, 0)
    ddl = str(CreateTable(ChannelSurface.__table__).compile(dialect=dialect))
    assert "GENERATED ALWAYS AS" in ddl and "STORED" in ddl


def test_connector_scoped_channels_can_share_external_identity(database):
    """Two instance channels coexist; duplicates in one instance fail."""
    with database.session() as session:
        a, b = _connector(session), _connector(session, "fluxer-b")
        for connector in (a, b):
            session.add(
                ChannelSurface(
                    connector_id=connector.id, external_channel_id="900", kind="forum"
                )
            )
        session.flush()
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                ChannelSurface(
                    connector_id=a.id, external_channel_id="900", kind="forum"
                )
            )
            session.flush()
        assert len(session.scalars(select(ChannelSurface)).all()) == 2


def test_message_cannot_move_away_from_its_parent_thread_channel(database):
    """Message and thread share one physical copy even across equal external IDs."""
    with database.session() as session:
        a, b = _connector(session), _connector(session, "fluxer-b")
        channels = [
            ChannelSurface(
                connector_id=c.id, external_channel_id=external, kind="forum"
            )
            for c, external in [(a, "900"), (a, "901"), (b, "900")]
        ]
        session.add_all(channels)
        session.flush()
        thread = LocalCommunityThreadSurface(
            local_community_thread_id=1,
            channel_surface_id=channels[0].id,
            connector_id=a.id,
            external_thread_id="111",
            external_starter_message_id="222",
            role="host",
        )
        session.add(thread)
        session.flush()
        message = LocalCommunityMessageSurface(
            local_community_message_id=1,
            local_community_thread_surface_id=thread.id,
            channel_surface_id=channels[0].id,
            connector_id=a.id,
            external_message_id="333",
            role="host",
        )
        session.add(message)
        session.flush()
        for channel in channels[1:]:
            with pytest.raises(IntegrityError), session.begin_nested():
                session.execute(
                    text(
                        "UPDATE local_community_message_surfaces SET channel_surface_id=:surface,connector_id=:connector WHERE id=:id"
                    ),
                    {
                        "surface": channel.id,
                        "connector": channel.connector_id,
                        "id": message.id,
                    },
                )
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(
                text(
                    "UPDATE local_community_thread_surfaces SET channel_surface_id=:channel WHERE id=:id"
                ),
                {"channel": channels[1].id, "id": thread.id},
            )
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []


def test_mixed_connectors_keep_global_follow_count_but_scope_discord_recipients(
    database,
):
    """Accepted Fluxer persists globally without becoming a Discord target."""
    from src.db.repositories.platform_surfaces import PlatformSurfaceRepository

    discord = database.remote_subscriptions.create_subscription(
        discord_channel_id=900,
        lemmy_community_actor_id="https://remote.test/c/news",
        lemmy_community_name="news",
        lemmy_community_id=None,
        status="accepted",
    )
    with database.immediate_session() as session:
        connector = _connector(session)
        row = ChannelCommunitySubscription(
            lemmy_community_actor_id=discord.lemmy_community_actor_id, status="accepted"
        )
        surface = ChannelSurface(
            connector_id=connector.id, external_channel_id="900", kind="forum"
        )
        session.add_all([row, surface])
        session.flush()
        PlatformSurfaceRepository.claim_remote_subscription(session, surface.id, row.id)
    assert (
        database.remote_subscriptions.count_subscriptions_for_community(
            discord.lemmy_community_actor_id
        )
        == 2
    )
    assert len(database.remote_subscriptions.list_subscriptions(status="accepted")) == 2
    assert [
        r.id
        for r in database.remote_subscriptions.list_discord_accepted_subscriptions_for_community(
            discord.lemmy_community_actor_id
        )
    ] == [discord.id]
    assert [
        r.id for r in database.remote_subscriptions.list_discord_subscriptions()
    ] == [discord.id]
    assert database.remote_subscriptions.delete_subscription(900)
    assert (
        database.remote_subscriptions.count_subscriptions_for_community(
            discord.lemmy_community_actor_id
        )
        == 1
    )
    assert (
        database.remote_subscriptions.list_discord_accepted_subscriptions_for_community(
            discord.lemmy_community_actor_id
        )
        == []
    )


def test_surface_boundary_rejects_wrong_projection_and_foreign_claim(database):
    """A claim validates projections and exclusivity before committing domain changes."""
    from src.db.repositories.platform_surfaces import PlatformSurfaceRepository

    with database.immediate_session() as session:
        connector = _connector(session)
        surface = ChannelSurface(
            connector_id=connector.id, external_channel_id="900", kind="forum"
        )
        row = ChannelCommunitySubscription(
            discord_channel_id=900,
            lemmy_community_actor_id="https://remote.test/c/news",
        )
        session.add_all([surface, row])
        session.flush()
        with pytest.raises(ValueError, match="projection"):
            PlatformSurfaceRepository.claim_remote_subscription(
                session, surface.id, row.id
            )
        row.discord_channel_id = None
        session.flush()
        PlatformSurfaceRepository.claim_remote_subscription(session, surface.id, row.id)
        subscriber = LocalSubscriber(local_community_id=1)
        session.add(subscriber)
        session.flush()
        with pytest.raises(ValueError, match="owned"):
            PlatformSurfaceRepository.claim_local_subscriber(
                session, surface.id, subscriber.id
            )
        assert (
            surface.remote_subscription_id == row.id
            and surface.role == "remote_subscription"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["denied", "failed", "pending", "recreated"])
async def test_failed_retry_preserves_identity_and_claim(database, outcome):
    """A moderator retry never releases or replaces a failed subscription owner."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from discordops import run_operation_definition_async
    from src.operations import SubscribeInput, subscribe_operation
    from support.runtime import build_test_policy_service

    with database.session() as session:
        _owners(session)
    actor = "https://remote.test/c/retry"
    follow = database.bridge_actor_follows.create_bridge_actor_follow(
        community_actor_id=actor,
        follow_activity_id=None,
        community_inbox_url=None,
        status="failed",
    )
    subscription = database.remote_subscriptions.create_subscription(
        discord_channel_id=900,
        lemmy_community_actor_id=actor,
        lemmy_community_name="retry",
        lemmy_community_id=None,
        status="failed",
    )
    surface_before = database.platform_surfaces.resolve_channel(
        "discord-primary", "900"
    )
    gateway = SimpleNamespace(
        follow_community=AsyncMock(
            return_value=SimpleNamespace(
                community_actor_url=actor,
                follow_activity_id="https://bridge.test/follow/retry",
                community_inbox_url=actor + "/inbox",
            )
        )
    )
    if outcome == "failed":
        gateway.follow_community.side_effect = RuntimeError("network failure")
    replacement = None
    if outcome == "recreated":

        async def recreate(*_):
            """Simulate unsubscribe and a new failed owner during the network await."""
            nonlocal replacement
            assert database.remote_subscriptions.delete_subscription(900)
            replacement = database.remote_subscriptions.create_subscription(
                discord_channel_id=900,
                lemmy_community_actor_id=actor,
                lemmy_community_name="retry",
                lemmy_community_id=None,
                status="failed",
            )
            return SimpleNamespace(
                community_actor_url=actor,
                follow_activity_id="https://bridge.test/follow/retry",
                community_inbox_url=actor + "/inbox",
            )

        gateway.follow_community.side_effect = recreate
    policy = build_test_policy_service(database)
    if outcome == "denied":
        policy = SimpleNamespace(
            federation_decision=lambda _: SimpleNamespace(allowed=False)
        )
    result = await run_operation_definition_async(
        subscribe_operation,
        SubscribeInput(
            database=database,
            fedify_gateway=gateway,
            discord_user_id="1",
            channel_id=900,
            channel_mention="<#900>",
            actor_id=actor,
            community_name="retry",
            numeric_id=None,
            community_handle="!retry@remote.test",
            policy_service=policy,
        ),
    )
    actual = database.remote_subscriptions.get_subscription_by_channel(900)
    actual_follow = database.bridge_actor_follows.get_bridge_actor_follow(actor)
    surface_after = database.platform_surfaces.resolve_channel("discord-primary", "900")
    expected = replacement if outcome == "recreated" else subscription
    assert actual.id == expected.id and actual.created_at.replace(
        tzinfo=None
    ) == expected.created_at.replace(tzinfo=None)
    assert actual_follow.id == follow.id
    assert (
        surface_before.id == surface_after.id
        and surface_after.role == "remote_subscription"
    )
    assert actual.status == ("pending" if outcome == "pending" else "failed")
    assert result.applied == (outcome == "pending")
    if outcome == "denied":
        gateway.follow_community.assert_not_awaited()


def test_connector_origin_and_personal_projection_boundaries(database):
    """Canonical origins and complete nullable projections admit valid rows only."""
    repo = database.platform_surfaces
    with database.immediate_session() as session:
        connector = repo.add_connector(
            session,
            connector_key="origin-test",
            platform="fluxer",
            instance_origin="HTTPS://EXAMPLE.test:8443",
        )
        assert connector.instance_origin == "https://example.test:8443"
        for origin in (
            "https://u:p@example.test",
            "https://example.test/path",
            "https://example.test?q=1",
            "https://example.test#fragment",
            "file://example.test",
        ):
            with pytest.raises(ValueError):
                repo.add_connector(
                    session,
                    connector_key="bad-origin",
                    platform="fluxer",
                    instance_origin=origin,
                )
        owners = _owners(session)
        binding = session.get(PersonalFeedBinding, owners["personal_feed_binding_id"])
        assert binding.discord_guild_id is None and binding.discord_channel_id is None
        for guild, channel in ((1, None), (None, 900)):
            with pytest.raises(IntegrityError), session.begin_nested():
                binding.discord_guild_id = guild
                binding.discord_channel_id = channel
                session.flush()
        binding.discord_guild_id, binding.discord_channel_id = 1, 900
        session.flush()


def test_neutral_local_subscriber_claim_and_conflict(database):
    """Fluxer subscribers need no Discord IDs and cannot steal an occupied forum."""
    with database.immediate_session() as session:
        owners = _owners(session)
        connector = _connector(session)
        surface = database.platform_surfaces.ensure_channel(
            session,
            connector_key=connector.connector_key,
            external_channel_id="opaque-forum",
            kind="forum",
        )
        row = database.local_subscribers.add_local_subscriber(
            session,
            surface.id,
            local_community_id=owners["local_community_id"],
            status="active",
        )
        assert row.discord_channel_id is None and row.discord_guild_id is None
        session.refresh(surface)
        assert (
            surface.local_subscriber_id == row.id and surface.role == "local_subscriber"
        )
        with pytest.raises(ValueError), session.begin_nested():
            database.local_subscribers.add_local_subscriber(
                session,
                surface.id,
                local_community_id=owners["local_community_id"],
                status="active",
            )
