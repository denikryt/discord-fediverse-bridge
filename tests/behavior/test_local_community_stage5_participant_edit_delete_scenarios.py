"""Stage 5 participant-wide edit/delete scenarios for local communities.

These tests cover the mutation paths that Stage 5 adds on top of the Stage 4
create model: host, local-subscriber, and remote-originated updates/deletes must
mutate the persisted sibling surfaces for the same canonical activity without
creating new canonical rows or relying on source-message-only ownership.
"""

from __future__ import annotations
from support.runtime import build_test_policy_service

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.activitypub_handlers import dispatch_activitypub_event
from src.activitypub_models import ActivityPubEvent
from src.content_publish_service import ContentPublishService
from src.local_communities.runtime import LocalCommunityRuntime
from src.local_communities.service import LocalCommunityService
from support.db import add_registered_user, build_database
from support.runtime import build_runtime_namespace


class EditableMessage:
    """Fake Discord message that records edit attempts and can fail on demand."""

    def __init__(self, *, message_id: int, content: str = "old", fail: bool = False) -> None:
        """Create one editable fake message for thread fetch scenarios."""
        self.id = message_id
        self.content = content
        self.edit = AsyncMock(side_effect=RuntimeError("edit failed") if fail else None)


class EditableThread:
    """Fake Discord thread that returns editable messages by message id."""

    def __init__(self, *, thread_id: int, messages: dict[int, EditableMessage]) -> None:
        """Create one fake thread with a surface-local message lookup table."""
        self.id = thread_id
        self.messages = messages
        self.fetch_message = AsyncMock(side_effect=self._fetch_message)

    async def _fetch_message(self, message_id: int) -> EditableMessage:
        """Return one fake Discord message or fail like a missing platform edge."""
        return self.messages[message_id]


class EditableBot:
    """Fake bot exposing only the thread lookup needed by edit/delete fanout."""

    def __init__(self, *, threads: dict[int, EditableThread]) -> None:
        """Create one bot fake keyed by Discord thread id."""
        self.threads = threads

    async def get_thread_by_id(self, thread_id: int) -> EditableThread:
        """Return one fake thread for the requested id."""
        return self.threads[thread_id]


@pytest.fixture()
def scenario(tmp_path: Path) -> SimpleNamespace:
    """Build a local-community runtime with one host and two local subscribers."""
    database = build_database(tmp_path, "stage5-local-subscriber-mutations.db")
    gateway = AsyncMock()
    publish_service = ContentPublishService(
        database=database,
        fedify_gateway=gateway,
        bridge_prefix="[bridge]",
            bridge_policy_service=build_test_policy_service(database),
)
    runtime = LocalCommunityRuntime(
        database=database,
        fedify_gateway=gateway,
        content_publish_service=publish_service,
        bridge_prefix="[bridge]",
            bridge_policy_service=build_test_policy_service(database),
)
    LocalCommunityService(
        database=database,
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
    community = database.local_communities.get_local_community_by_slug("hackers")
    source = database.local_subscribers.create_local_subscriber(
        local_community_id=community.id,
        discord_guild_id=10,
        discord_channel_id=200,
        initiated_by_discord_user_id="999",
        status="active",
    )
    sibling = database.local_subscribers.create_local_subscriber(
        local_community_id=community.id,
        discord_guild_id=10,
        discord_channel_id=300,
        initiated_by_discord_user_id="999",
        status="active",
    )
    add_registered_user(database)
    return SimpleNamespace(
        database=database,
        runtime=runtime,
        gateway=gateway,
        community=community,
        source=source,
        sibling=sibling,
            bridge_policy_service=build_test_policy_service(database),
)


def _thread_with_surfaces(scenario: SimpleNamespace) -> object:
    """Create one canonical post with host/source/sibling Discord surfaces."""
    thread_row = scenario.database.local_community_content.create_local_community_thread_canonical(
        local_community_id=scenario.community.id,
        ap_activity_id="https://bridge.example/activities/create/post/1",
        ap_object_id="https://bridge.example/post/1",
        direction="discord_to_ap",
        origin_kind="discord_local_subscriber",
    )
    scenario.database.local_community_surfaces.create_local_community_thread_surface(
        local_community_thread_id=thread_row.id,
        discord_forum_channel_id=100,
        discord_thread_id=1100,
        discord_starter_message_id=1200,
        role="host",
        local_subscriber_id=None,
    )
    scenario.database.local_community_surfaces.create_local_community_thread_surface(
        local_community_thread_id=thread_row.id,
        discord_forum_channel_id=200,
        discord_thread_id=2200,
        discord_starter_message_id=2300,
        role="local_subscriber",
        local_subscriber_id=scenario.source.id,
    )
    scenario.database.local_community_surfaces.create_local_community_thread_surface(
        local_community_thread_id=thread_row.id,
        discord_forum_channel_id=300,
        discord_thread_id=3300,
        discord_starter_message_id=3400,
        role="local_subscriber",
        local_subscriber_id=scenario.sibling.id,
    )
    scenario.database.activitypub_objects.create_published_activity_object(
        actor_username="alice",
        actor_url="https://bridge.example/users/alice",
        community_actor_url=scenario.community.actor_url,
        activity_id="https://bridge.example/activities/create/post/1",
        object_id="https://bridge.example/post/1",
        kind="post",
        title="Thread title",
        body_markdown="old body",
        in_reply_to_object_id=None,
        discord_channel_id=200,
        discord_message_id=2300,
    )
    return thread_row


def _comment_with_surfaces(scenario: SimpleNamespace, thread_row: object) -> object:
    """Create one canonical comment with host/source/sibling Discord surfaces."""
    host_thread = scenario.database.local_community_surfaces.get_local_community_thread_surface(
        local_community_thread_id=thread_row.id,
        discord_forum_channel_id=100,
    )
    source_thread = scenario.database.local_community_surfaces.get_local_community_thread_surface(
        local_community_thread_id=thread_row.id,
        discord_forum_channel_id=200,
    )
    sibling_thread = scenario.database.local_community_surfaces.get_local_community_thread_surface(
        local_community_thread_id=thread_row.id,
        discord_forum_channel_id=300,
    )
    message_row = scenario.database.local_community_content.create_local_community_message_canonical(
        local_community_thread_id=thread_row.id,
        ap_activity_id="https://bridge.example/activities/create/comment/1",
        ap_object_id="https://bridge.example/comment/1",
        parent_ap_object_id="https://bridge.example/post/1",
        direction="discord_to_ap",
    )
    scenario.database.local_community_surfaces.create_local_community_message_surface(
        local_community_message_id=message_row.id,
        local_community_thread_surface_id=host_thread.id,
        discord_forum_channel_id=100,
        discord_message_id=1300,
        parent_discord_message_id=1200,
        role="host",
        local_subscriber_id=None,
    )
    scenario.database.local_community_surfaces.create_local_community_message_surface(
        local_community_message_id=message_row.id,
        local_community_thread_surface_id=source_thread.id,
        discord_forum_channel_id=200,
        discord_message_id=2400,
        parent_discord_message_id=2300,
        role="local_subscriber",
        local_subscriber_id=scenario.source.id,
    )
    scenario.database.local_community_surfaces.create_local_community_message_surface(
        local_community_message_id=message_row.id,
        local_community_thread_surface_id=sibling_thread.id,
        discord_forum_channel_id=300,
        discord_message_id=3500,
        parent_discord_message_id=3400,
        role="local_subscriber",
        local_subscriber_id=scenario.sibling.id,
    )
    scenario.database.activitypub_objects.create_published_activity_object(
        actor_username="alice",
        actor_url="https://bridge.example/users/alice",
        community_actor_url=scenario.community.actor_url,
        activity_id="https://bridge.example/activities/create/comment/1",
        object_id="https://bridge.example/comment/1",
        kind="comment",
        title=None,
        body_markdown="old comment",
        in_reply_to_object_id="https://bridge.example/post/1",
        discord_channel_id=200,
        discord_message_id=2400,
    )
    return message_row


def _bot_for_surfaces(*, failing_message_ids: set[int] | None = None) -> tuple[EditableBot, dict[int, EditableMessage]]:
    """Create an editable bot covering all host/source/sibling message ids."""
    failing_message_ids = failing_message_ids or set()
    messages = {
        message_id: EditableMessage(message_id=message_id, fail=message_id in failing_message_ids)
        for message_id in (1200, 1300, 2300, 2400, 3400, 3500)
    }
    bot = EditableBot(
        threads={
            1100: EditableThread(thread_id=1100, messages=messages),
            2200: EditableThread(thread_id=2200, messages=messages),
            3300: EditableThread(thread_id=3300, messages=messages),
        }
    )
    return bot, messages


def _runtime_namespace(scenario: SimpleNamespace, bot: EditableBot) -> SimpleNamespace:
    """Build the runtime namespace used by raw edit/delete handlers."""
    return build_runtime_namespace(
        fedify_gateway=scenario.gateway,
        database=scenario.database,
        local_community_runtime=scenario.runtime,
        community_runtime=SimpleNamespace(),
        bot=bot,
            bridge_policy_service=build_test_policy_service(scenario.database),
)


def _remote_post_event(event_type: str) -> ActivityPubEvent:
    """Build one normalized remote post update/delete event for dispatch tests."""
    return ActivityPubEvent.model_validate(
        {
            "event_type": event_type,
            "delivery_id": f"https://remote.example/activities/{event_type}/post/1",
            "occurred_at": "2026-05-19T10:00:00Z",
            "community_actor_id": "https://bridge.example/communities/hackers",
            "actor_id": "https://remote.example/u/bob",
            "object": {
                "ap_id": "https://bridge.example/post/1",
                "kind": "post",
                "lemmy_id": 1,
                "post_ap_id": None,
                "post_lemmy_id": None,
                "parent_ap_id": None,
                "title": "Remote title",
                "body_markdown": "remote body",
                "url": "https://remote.example/post/1",
                "published_at": "2026-05-19T10:00:00Z",
                "author_name": "bob",
            },
        }
    )


def _remote_comment_event(event_type: str) -> ActivityPubEvent:
    """Build one normalized remote comment update/delete event for dispatch tests."""
    return ActivityPubEvent.model_validate(
        {
            "event_type": event_type,
            "delivery_id": f"https://remote.example/activities/{event_type}/comment/1",
            "occurred_at": "2026-05-19T10:05:00Z",
            "community_actor_id": "https://bridge.example/communities/hackers",
            "actor_id": "https://remote.example/u/bob",
            "object": {
                "ap_id": "https://bridge.example/comment/1",
                "kind": "comment",
                "lemmy_id": 2,
                "post_ap_id": "https://bridge.example/post/1",
                "post_lemmy_id": 1,
                "parent_ap_id": "https://bridge.example/post/1",
                "title": None,
                "body_markdown": "remote comment",
                "url": "https://remote.example/comment/1",
                "published_at": "2026-05-19T10:05:00Z",
                "author_name": "bob",
            },
        }
    )


@pytest.mark.asyncio
async def test_host_starter_edit_updates_subscriber_starter_surfaces(scenario: SimpleNamespace) -> None:
    """Host starter edits should AP-update once and edit subscriber copies."""
    _thread_with_surfaces(scenario)
    bot, messages = _bot_for_surfaces()
    scenario.runtime.bot = bot

    await scenario.runtime.handle_discord_message_edit(
        message_id=1200,
        new_content="host edit",
        runtime=_runtime_namespace(scenario, bot),
    )

    scenario.gateway.update_content.assert_awaited_once()
    assert scenario.gateway.update_content.await_args.args[0].ap_object_id == "https://bridge.example/post/1"
    messages[2300].edit.assert_awaited_once_with(content="host edit")
    messages[3400].edit.assert_awaited_once_with(content="host edit")
    messages[1200].edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_subscriber_starter_edit_updates_host_and_sibling_surfaces(scenario: SimpleNamespace) -> None:
    """Local-subscriber starter edits should become canonical AP updates."""
    _thread_with_surfaces(scenario)
    bot, messages = _bot_for_surfaces()
    scenario.runtime.bot = bot

    await scenario.runtime.handle_discord_message_edit(
        message_id=2300,
        new_content="subscriber edit",
        runtime=_runtime_namespace(scenario, bot),
    )

    scenario.gateway.update_content.assert_awaited_once()
    assert scenario.gateway.update_content.await_args.args[0].ap_object_id == "https://bridge.example/post/1"
    messages[1200].edit.assert_awaited_once_with(content="subscriber edit")
    messages[3400].edit.assert_awaited_once_with(content="subscriber edit")
    messages[2300].edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_host_and_local_subscriber_comment_edits_update_other_surfaces(scenario: SimpleNamespace) -> None:
    """Comment edits should fan out by canonical message surface mapping."""
    thread_row = _thread_with_surfaces(scenario)
    _comment_with_surfaces(scenario, thread_row)
    bot, messages = _bot_for_surfaces()
    scenario.runtime.bot = bot

    await scenario.runtime.handle_discord_message_edit(
        message_id=1300,
        new_content="host comment edit",
        runtime=_runtime_namespace(scenario, bot),
    )
    messages[2400].edit.assert_awaited_once_with(content="host comment edit")
    messages[3500].edit.assert_awaited_once_with(content="host comment edit")
    messages[1300].edit.assert_not_awaited()

    scenario.gateway.update_content.reset_mock()
    for message in messages.values():
        message.edit.reset_mock()
    await scenario.runtime.handle_discord_message_edit(
        message_id=2400,
        new_content="subscriber comment edit",
        runtime=_runtime_namespace(scenario, bot),
    )

    scenario.gateway.update_content.assert_awaited_once()
    assert scenario.gateway.update_content.await_args.args[0].ap_object_id == "https://bridge.example/comment/1"
    messages[1300].edit.assert_awaited_once_with(content="subscriber comment edit")
    messages[3500].edit.assert_awaited_once_with(content="subscriber comment edit")
    messages[2400].edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_discord_deletes_mark_non_source_surfaces(scenario: SimpleNamespace) -> None:
    """Host and local-subscriber deletes should mark every non-source copy."""
    thread_row = _thread_with_surfaces(scenario)
    _comment_with_surfaces(scenario, thread_row)
    bot, messages = _bot_for_surfaces()
    scenario.runtime.bot = bot

    await scenario.runtime.handle_discord_message_delete(
        message_id=1200,
        runtime=_runtime_namespace(scenario, bot),
    )
    scenario.gateway.delete_content.assert_awaited_once()
    messages[2300].edit.assert_awaited_once_with(content="*deleted by creator*")
    messages[3400].edit.assert_awaited_once_with(content="*deleted by creator*")
    messages[1200].edit.assert_not_awaited()

    scenario.gateway.delete_content.reset_mock()
    for message in messages.values():
        message.edit.reset_mock()
    await scenario.runtime.handle_discord_message_delete(
        message_id=2400,
        runtime=_runtime_namespace(scenario, bot),
    )

    scenario.gateway.delete_content.assert_awaited_once()
    assert scenario.gateway.delete_content.await_args.args[0].ap_object_id == "https://bridge.example/comment/1"
    messages[1300].edit.assert_awaited_once_with(content="*deleted by creator*")
    messages[3500].edit.assert_awaited_once_with(content="*deleted by creator*")
    messages[2400].edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbound_remote_updates_edit_all_local_surfaces(scenario: SimpleNamespace) -> None:
    """Remote post/comment updates should edit host and local subscriber copies."""
    thread_row = _thread_with_surfaces(scenario)
    _comment_with_surfaces(scenario, thread_row)
    bot, messages = _bot_for_surfaces()
    scenario.runtime.bot = bot
    runtime = _runtime_namespace(scenario, bot)

    post_result = await dispatch_activitypub_event(_remote_post_event("post.updated"), runtime)
    comment_result = await dispatch_activitypub_event(_remote_comment_event("comment.updated"), runtime)

    assert post_result.status == "processed"
    assert comment_result.status == "processed"
    for message_id in (1200, 2300, 3400):
        messages[message_id].edit.assert_any_await(
            content="**Remote title**\n\nAuthor: `bob@remote.example`\n\nremote body\n\nhttps://remote.example/post/1"
        )
    for message_id in (1300, 2400, 3500):
        messages[message_id].edit.assert_any_await(content="`bob@remote.example`\n\nremote comment")


@pytest.mark.asyncio
async def test_inbound_remote_deletes_mark_all_local_surfaces(scenario: SimpleNamespace) -> None:
    """Remote deletes should mark every persisted local Discord surface."""
    thread_row = _thread_with_surfaces(scenario)
    _comment_with_surfaces(scenario, thread_row)
    bot, messages = _bot_for_surfaces()
    scenario.runtime.bot = bot
    runtime = _runtime_namespace(scenario, bot)

    await dispatch_activitypub_event(_remote_post_event("post.deleted"), runtime)
    await dispatch_activitypub_event(_remote_comment_event("comment.deleted"), runtime)

    for message_id in (1200, 2300, 3400, 1300, 2400, 3500):
        messages[message_id].edit.assert_any_await(content="*deleted by creator*")


@pytest.mark.asyncio
async def test_partial_local_discord_failure_does_not_block_healthy_targets(scenario: SimpleNamespace) -> None:
    """A failed Discord target should not block AP update or other surfaces."""
    _thread_with_surfaces(scenario)
    bot, messages = _bot_for_surfaces(failing_message_ids={1200})
    scenario.runtime.bot = bot

    await scenario.runtime.handle_discord_message_edit(
        message_id=2300,
        new_content="subscriber edit with one broken target",
        runtime=_runtime_namespace(scenario, bot),
    )

    scenario.gateway.update_content.assert_awaited_once()
    messages[1200].edit.assert_awaited_once()
    messages[3400].edit.assert_awaited_once_with(content="subscriber edit with one broken target")


@pytest.mark.asyncio
async def test_inactive_local_subscriber_source_mutation_is_contained(scenario: SimpleNamespace) -> None:
    """Surfaces from inactive local subscribers must not author mutations."""
    _thread_with_surfaces(scenario)
    bot, messages = _bot_for_surfaces()
    scenario.runtime.bot = bot
    scenario.database.local_subscribers.delete_local_subscriber(200)

    await scenario.runtime.handle_discord_message_edit(
        message_id=2300,
        new_content="should not propagate",
        runtime=_runtime_namespace(scenario, bot),
    )
    await scenario.runtime.handle_discord_message_delete(
        message_id=2300,
        runtime=_runtime_namespace(scenario, bot),
    )

    scenario.gateway.update_content.assert_not_awaited()
    scenario.gateway.delete_content.assert_not_awaited()
    for message in messages.values():
        message.edit.assert_not_awaited()
