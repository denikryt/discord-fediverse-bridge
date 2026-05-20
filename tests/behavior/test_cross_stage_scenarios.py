"""Cross-stage scenarios that exercise registration, follow, publish, and dedup together."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from src.activitypub_handlers import dispatch_activitypub_event
from src.activitypub_models import ActivityPubEvent, FollowLifecycleEvent
from src.commands import subscribe
from src.community_sync.runtime import CommunityRuntime
from src.db import Database
from src.discord_publish_service import DiscordPublishService
from src.fedify_gateway_client import PublishContentResult
from src.http_api import create_http_app
from src.registration_service import RegistrationService
from tests_constants import (
    BRIDGE_EXAMPLE_DOMAIN,
    BRIDGE_HOST_DOMAIN,
    DISCORD_CDN_DOMAIN,
    DISCORD_EXAMPLE_DOMAIN,
    LEMMY_EXAMPLE_DOMAIN,
)


class FakeDiscordOAuthClient:
    """Provide one deterministic Discord identity for end-to-end registration."""

    def __init__(self) -> None:
        self.last_state: str | None = None

    def build_authorization_url(self, state: str) -> str:
        """Return one stable authorize URL so the registration client can redirect."""
        self.last_state = state
        return f"https://{DISCORD_EXAMPLE_DOMAIN}/oauth?state={state}"

    async def exchange_code_for_access_token(self, code: str) -> str:
        """Return one fake token so no external Discord call is needed."""
        assert code == "oauth-code"
        return "discord-access-token"

    async def fetch_user_profile(self, access_token: str) -> SimpleNamespace:
        """Return one Discord user profile that matches the publish scenario author."""
        assert access_token == "discord-access-token"
        return SimpleNamespace(
            user_id="1234567890",
            username="denchik",
            avatar_url=f"https://{DISCORD_CDN_DOMAIN}/avatar.png",
        )


def _database(tmp_path: Path) -> Database:
    """Create one real SQLite repository for cross-stage scenarios."""
    database = Database(f"sqlite:///{tmp_path / 'behavior-cross-stage.db'}")
    database.create_all()
    return database


def _registration_runtime(database: Database) -> SimpleNamespace:
    """Build the FastAPI runtime used by cross-stage registration steps."""
    settings = SimpleNamespace(
        fedify_shared_secret="secret",
        normalized_public_bridge_base_url=f"https://{BRIDGE_HOST_DOMAIN}",
        registration_session_cookie_name="bridge_registration_session",
        registration_session_ttl_seconds=3600,
    )
    registration_service = RegistrationService(
        database=database,
        base_url=settings.normalized_public_bridge_base_url,
        keypair_generator=lambda: ("test-public-key", "test-private-key"),
    )
    publish_service = DiscordPublishService(
        database=database,
        fedify_gateway=AsyncMock(),
        bridge_prefix="[bridge]",
    )
    community_runtime = CommunityRuntime(
        database=database,
        discord_publish_service=publish_service,
    )
    return SimpleNamespace(
        settings=settings,
        database=database,
        registration_service=registration_service,
        discord_oauth_client=FakeDiscordOAuthClient(),
        fedify_gateway=SimpleNamespace(),
        bot=SimpleNamespace(),
        community_runtime=community_runtime,
    )


def _register_user(client: TestClient, database: Database, *, username: str) -> None:
    """Drive the real registration HTTP flow to create one local actor."""
    client.get("/register")
    client.get("/auth/discord/start", follow_redirects=False)
    session_token = client.cookies.get("bridge_registration_session")
    session = database.get_registration_session_by_token(session_token)
    assert session is not None
    client.get(
        f"/auth/discord/callback?code=oauth-code&state={session.oauth_state}",
        follow_redirects=False,
    )
    response = client.post(
        "/register/complete",
        content=f"username={username}",
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _thread() -> SimpleNamespace:
    """Return one fake forum thread that matches the accepted subscription channel."""
    return SimpleNamespace(id=200, parent_id=12345, name="Thread title")


def _starter_message() -> SimpleNamespace:
    """Return one fake Discord starter message authored by the registered user."""
    return SimpleNamespace(
        id=300,
        content="hello from discord",
        author=SimpleNamespace(id=1234567890, display_name="Denchik", name="denchik"),
        reply=AsyncMock(),
    )


def _follow_accept_event(follow_activity_id: str) -> FollowLifecycleEvent:
    """Build the typed follow-acceptance event used in cross-stage lifecycle tests."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    return FollowLifecycleEvent(
        event_type="follow.accepted",
        delivery_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/accept/1",
        occurred_at=datetime.now(UTC),
        community_actor_id=community_actor_url,
        actor_id=community_actor_url,
        object={"follow_activity_id": follow_activity_id},
    )


def _echo_post_event(*, object_id: str, delivery_id: str) -> ActivityPubEvent:
    """Build the looped-back AP post event used to verify echo suppression."""
    return ActivityPubEvent.model_validate(
        {
            "event_type": "post.created",
            "delivery_id": delivery_id,
            "occurred_at": "2026-05-08T10:00:00Z",
            "community_actor_id": f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
            "actor_id": f"https://{LEMMY_EXAMPLE_DOMAIN}/u/alice",
            "object": {
                "ap_id": object_id,
                "kind": "post",
                "lemmy_id": 111,
                "post_ap_id": None,
                "post_lemmy_id": None,
                "parent_ap_id": None,
                "title": "Looped post",
                "body_markdown": "hello",
                "url": object_id,
                "published_at": "2026-05-08T10:00:00Z",
                "author_name": "alice",
            },
        }
    )


@pytest.mark.asyncio
async def test_register_subscribe_accept_publish_then_echo_is_suppressed(
    tmp_path: Path,
    command_tree,
    interaction,
    forum_channel,
    lemmy,
    fedify_gateway,
) -> None:
    """A fully registered and accepted flow should publish once and skip the loopback echo."""
    database = _database(tmp_path)
    client = TestClient(create_http_app(_registration_runtime(database)))
    _register_user(client, database, username="alice")

    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    follow_activity_id = f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1"
    fedify_gateway.follow_community.return_value = SimpleNamespace(
        community_actor_url=community_actor_url,
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=follow_activity_id,
    )
    subscribe.register(command_tree, database, fedify_gateway)
    subscribe_command = command_tree.commands["subscribe-channel"]
    await subscribe_command.callback(
        interaction,
        f"https://{LEMMY_EXAMPLE_DOMAIN}",
        f"{community_actor_url}|hackers|777",
        forum_channel,
    )
    pending_subscription = database.get_subscription_by_channel(forum_channel.id)
    assert pending_subscription is not None
    assert pending_subscription.status == "pending"

    follow_result = await dispatch_activitypub_event(
        _follow_accept_event(follow_activity_id),
        SimpleNamespace(
            database=database,
            bot=SimpleNamespace(
                fetch_user=AsyncMock(
                    return_value=SimpleNamespace(send=AsyncMock())
                )
            ),
        ),
    )
    accepted_subscription = database.get_subscription_by_channel(forum_channel.id)
    assert follow_result.status == "processed"
    assert accepted_subscription is not None
    assert accepted_subscription.status == "accepted"

    publish_gateway = AsyncMock()
    publish_gateway.publish_content.return_value = PublishContentResult(
        activity_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/post/1",
        object_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/1",
        community_actor_url=community_actor_url,
    )
    publish_service = DiscordPublishService(
        database=database,
        fedify_gateway=publish_gateway,
        bridge_prefix="[bridge]",
    )
    publish_result = await publish_service.publish_thread_starter(
        thread=_thread(),
        starter_message=_starter_message(),
    )

    echo_publish_service = DiscordPublishService(
        database=database,
        fedify_gateway=AsyncMock(),
        bridge_prefix="[bridge]",
    )
    echo_runtime = SimpleNamespace(
        database=database,
        bot=SimpleNamespace(
            wait_until_bridge_ready=AsyncMock(),
            fetch_forum_channel=AsyncMock(),
        ),
        community_runtime=CommunityRuntime(
            database=database,
            discord_publish_service=echo_publish_service,
        ),
    )
    echo_result = await dispatch_activitypub_event(
        _echo_post_event(
            object_id=publish_result.object_id or "",
            delivery_id=publish_result.activity_id or "",
        ),
        echo_runtime,
    )

    assert publish_result.status == "published"
    assert echo_result.status == "skipped"
    assert echo_result.detail == "discord-originated echo"
    echo_runtime.bot.fetch_forum_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_lemmy_announce_of_local_post_is_suppressed_via_actor_check(
    tmp_path: Path,
) -> None:
    """Lemmy Announce wraps our outbound post with a new ap_id — suppress via actor_id check.

    When Lemmy announces our Create(Page) back to us, it generates a new activity id,
    so the delivery_id and object_id lookups both miss. The actor_id check (is this one
    of our registered users?) must catch the echo.
    """
    database = _database(tmp_path)
    client = TestClient(create_http_app(_registration_runtime(database)))
    _register_user(client, database, username="alice")

    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    object_id = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/post/9999"
    actor_url = f"https://{BRIDGE_HOST_DOMAIN}/actors/alice"

    # Simulate Lemmy Announce: delivery_id and object_id are new Lemmy-generated ids,
    # but actor_id is our own registered user.
    lemmy_rewritten_event = ActivityPubEvent.model_validate(
        {
            "event_type": "post.created",
            "delivery_id": f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/announce/create/abc123",
            "occurred_at": "2026-05-08T10:00:00Z",
            "community_actor_id": community_actor_url,
            "actor_id": actor_url,
            "object": {
                "ap_id": f"https://{LEMMY_EXAMPLE_DOMAIN}/post/999",
                "kind": "post",
                "lemmy_id": 999,
                "post_ap_id": None,
                "post_lemmy_id": None,
                "parent_ap_id": None,
                "title": "Rewritten by Lemmy",
                "body_markdown": "hello",
                "url": object_id,
                "published_at": "2026-05-08T10:00:00Z",
                "author_name": "alice",
            },
        }
    )

    announce_publish_service = DiscordPublishService(
        database=database,
        fedify_gateway=AsyncMock(),
        bridge_prefix="[bridge]",
    )
    runtime = SimpleNamespace(
        database=database,
        bot=SimpleNamespace(
            wait_until_bridge_ready=AsyncMock(),
            fetch_forum_channel=AsyncMock(),
        ),
        community_runtime=CommunityRuntime(
            database=database,
            discord_publish_service=announce_publish_service,
        ),
    )
    result = await dispatch_activitypub_event(lemmy_rewritten_event, runtime)

    assert result.status == "skipped"
    assert result.detail == "discord-originated echo"
    runtime.bot.fetch_forum_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_subscribe_retry_then_accept_allows_publish(
    tmp_path: Path,
    command_tree,
    interaction,
    forum_channel,
    lemmy,
    fedify_gateway,
) -> None:
    """A failed subscribe retry should recover into accepted state and enable publish."""
    database = _database(tmp_path)
    database.create_user(
        discord_user_id="1234567890",
        activitypub_username="alice",
        actor_url=f"https://{BRIDGE_HOST_DOMAIN}/users/alice",
        inbox_url=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/inbox",
        outbox_url=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/outbox",
        followers_url=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/followers",
        public_key_pem="public-key",
        private_key_pem="private-key",
    )

    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    subscribe.register(command_tree, database, fedify_gateway)
    subscribe_command = command_tree.commands["subscribe-channel"]

    fedify_gateway.follow_community.side_effect = RuntimeError("boom")
    await subscribe_command.callback(
        interaction,
        f"https://{LEMMY_EXAMPLE_DOMAIN}",
        f"{community_actor_url}|hackers|777",
        forum_channel,
    )
    failed_subscription = database.get_subscription_by_channel(forum_channel.id)
    assert failed_subscription is not None
    assert failed_subscription.status == "failed"

    interaction.response.send_message.reset_mock()
    fedify_gateway.follow_community.side_effect = None
    follow_activity_id = f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/2"
    fedify_gateway.follow_community.return_value = SimpleNamespace(
        community_actor_url=community_actor_url,
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=follow_activity_id,
    )
    await subscribe_command.callback(
        interaction,
        f"https://{LEMMY_EXAMPLE_DOMAIN}",
        f"{community_actor_url}|hackers|777",
        forum_channel,
    )
    pending_subscription = database.get_subscription_by_channel(forum_channel.id)
    assert pending_subscription is not None
    assert pending_subscription.status == "pending"

    await dispatch_activitypub_event(
        _follow_accept_event(follow_activity_id),
        SimpleNamespace(
            database=database,
            bot=SimpleNamespace(
                fetch_user=AsyncMock(
                    return_value=SimpleNamespace(send=AsyncMock())
                )
            ),
        ),
    )
    accepted_subscription = database.get_subscription_by_channel(forum_channel.id)
    assert accepted_subscription is not None
    assert accepted_subscription.status == "accepted"

    publish_gateway = AsyncMock()
    publish_gateway.publish_content.return_value = PublishContentResult(
        activity_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/post/2",
        object_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/2",
        community_actor_url=community_actor_url,
    )
    publish_service = DiscordPublishService(
        database=database,
        fedify_gateway=publish_gateway,
        bridge_prefix="[bridge]",
    )

    publish_result = await publish_service.publish_thread_starter(
        thread=_thread(),
        starter_message=_starter_message(),
    )
    mapping = database.get_message_mapping_by_discord_message_id(300)

    assert publish_result.status == "published"
    assert mapping is not None
