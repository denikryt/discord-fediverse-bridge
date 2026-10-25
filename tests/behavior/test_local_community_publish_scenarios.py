"""Behavior scenarios for Discord-originated local-community publishes."""

from __future__ import annotations

from support.community_events import create_thread, create_message, configure_renderer, source_ids

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from support.runtime import build_test_policy_service
from sqlalchemy import func, select

from src.content_publish_service import ContentPublishService
from src.fedify_gateway_client import FedifyGatewayClient
from src.local_communities.runtime import LocalCommunityRuntime
from src.local_communities.service import LocalCommunityService
from src.models import MessageMapping, PublishedActivityObject
from support.db import add_registered_user, build_database
from support.discord import build_starter_message, build_thread, build_thread_message


def _runtime(tmp_path: Path) -> tuple[object, LocalCommunityRuntime]:
    """Build a real local-community runtime with only the gateway mocked."""
    database = build_database(tmp_path, "local-community-publish.db")
    gateway = AsyncMock()
    configure_renderer(gateway, database)
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
    return database, runtime


def _local_community(database: object, *, channel_id: int = 100) -> None:
    """Seed one local community bound to the shared Discord forum channel."""
    LocalCommunityService(
        database=database,
        base_url="https://bridge.example",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=channel_id,
        slug="hackers",
        name="Hackers",
        description="A local hackerspace forum.",
        created_by_discord_user_id="123",
    )


def _real_gateway_runtime(
    tmp_path: Path,
) -> tuple[object, LocalCommunityRuntime, list[dict]]:
    """Build a runtime that exercises the real gateway client serialization."""

    requests: list[dict] = []

    async def capture(request: httpx.Request) -> httpx.Response:
        """Record the outbound payload and return one successful gateway response."""
        payload = json.loads(request.content)
        requests.append({"path": request.url.path, "payload": payload})
        from support.community_events import render_create
        from src.fedify_gateway_client import RenderCommunityRequest
        rendered = render_create(RenderCommunityRequest(
            actor_username=payload['actorUsername'], community_actor_url=payload['communityActorUrl'],
            kind=payload['kind'], title=payload['title'], body_markdown=payload['bodyMarkdown'],
            in_reply_to_object_id=payload['inReplyToObjectId'], operation_id=payload['operationId'],
            activity_id=payload['activityId'], object_id=payload['objectId'], published_at=payload['publishedAt'],
        ), database.users.get_user_by_activitypub_username(payload['actorUsername']).actor_url)
        return httpx.Response(200, json={
            'activityId': rendered.activity_id, 'objectId': rendered.object_id,
            'communityActorUrl': rendered.community_actor_url,
            'publishedAt': rendered.published_at, 'activityJson': rendered.activity_json,
        })

    database = build_database(tmp_path, "local-community-publish-real.db")
    gateway = object.__new__(FedifyGatewayClient)
    gateway._shared_secret = "secret"
    gateway._client = httpx.AsyncClient(
        base_url="http://gateway.test",
        transport=httpx.MockTransport(capture),
    )
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
    return database, runtime, requests


@pytest.mark.asyncio
async def test_registered_user_thread_starter_in_local_community_publishes_post(
    tmp_path: Path,
) -> None:
    """A local-community thread starter should publish one AP post and map the thread."""
    database, runtime = _runtime(tmp_path)
    _local_community(database)
    add_registered_user(database)
    runtime.fedify_gateway.publish_content.side_effect = AssertionError(
        "local-community thread starters must use the dedicated local-community publish path"
    )

    result = await create_thread(runtime, thread=build_thread(), starter_message=build_starter_message())
    thread_surface = database.local_community_surfaces.get_local_community_thread_surface_by_discord_thread_id(
        200
    )
    assert thread_surface is not None
    created = database.local_community_surfaces.get_local_community_thread_for_surface(
        thread_surface.id
    )

    assert result.status == "published"
    assert created is not None
    assert created.ap_object_id == source_ids(database, message=300, kind='post')[2]
    runtime.fedify_gateway.render_community.assert_awaited_once()
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(MessageMapping)) == 1
        assert (
            session.scalar(select(func.count()).select_from(PublishedActivityObject))
            == 1
        )


@pytest.mark.asyncio
async def test_unregistered_user_thread_starter_in_local_community_is_rejected(
    tmp_path: Path,
) -> None:
    """An unregistered author should not publish into a local community."""
    database, runtime = _runtime(tmp_path)
    _local_community(database)
    starter_message = build_starter_message(author_id=999)

    result = await create_thread(runtime, thread=build_thread(), starter_message=starter_message)

    assert result.reason == "unregistered_user"
    assert (
        database.local_community_surfaces.get_local_community_thread_surface_by_discord_thread_id(
            200
        )
        is None
    )
    starter_message.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_discord_reply_in_local_community_thread_publishes_comment_with_parent_mapping(
    tmp_path: Path,
) -> None:
    """A local-community reply should publish one AP comment linked to the post root."""
    database, runtime = _runtime(tmp_path)
    _local_community(database)
    add_registered_user(database)
    thread = build_thread()
    starter = build_starter_message()
    runtime.fedify_gateway.publish_content.side_effect = AssertionError(
        "local-community replies must use the dedicated local-community publish path"
    )
    await create_thread(runtime, thread=thread, starter_message=starter)

    result = await create_message(runtime, message=build_thread_message())
    message_surface = database.local_community_surfaces.get_local_community_message_surface_by_discord_message_id(
        301
    )
    assert message_surface is not None
    created = database.local_community_surfaces.get_local_community_message_for_surface(
        message_surface.id
    )

    assert result.status == "published"
    assert created is not None
    assert created.parent_ap_object_id == source_ids(database, message=300, kind='post')[2]
    assert runtime.fedify_gateway.render_community.await_count == 2


@pytest.mark.asyncio
async def test_local_community_reply_semantics_preserve_nested_and_unknown_fallback(
    tmp_path: Path,
) -> None:
    """Local-community replies should keep nested and unknown-parent semantics."""
    database, runtime = _runtime(tmp_path)
    _local_community(database)
    add_registered_user(database)
    thread = build_thread()
    starter = build_starter_message()
    await create_thread(runtime, thread=thread, starter_message=starter)
    await create_message(runtime, message=build_thread_message(message_id=301))

    nested_result = await create_message(runtime, message=build_thread_message(message_id=302, reference_message_id=301))
    unknown_result = await create_message(runtime, message=build_thread_message(message_id=303, reference_message_id=999))

    assert nested_result.status == "published"
    assert unknown_result.status == "published"
    nested_request = (
        runtime.fedify_gateway.render_community.await_args_list[2].args[
            0
        ]
    )
    unknown_request = (
        runtime.fedify_gateway.render_community.await_args_list[3].args[
            0
        ]
    )
    assert (
        nested_request.in_reply_to_object_id
        == source_ids(database, message=301, kind='comment')[2]
    )
    assert (
        unknown_request.in_reply_to_object_id
        == source_ids(database, message=300, kind='post')[2]
    )


@pytest.mark.asyncio
async def test_local_community_create_preserves_canonical_create_for_direct_follower_fanout(
    tmp_path: Path,
) -> None:
    """Real local runtime persists the exact embedded Create, not the community Announce."""
    from src.models import UserActorOutboundDelivery, UserActorOutboundSource

    database, runtime = _runtime(tmp_path)
    _local_community(database)
    add_registered_user(database)
    user = database.users.get_user_by_discord_user_id("123")
    assert user is not None
    with database.immediate_session() as session:
        database.user_actor_followers.create_or_refresh(
            session,
            user_id=user.id,
            remote_actor_id="https://remote.example/users/follower",
            remote_inbox_url="https://remote.example/users/follower/inbox",
            remote_shared_inbox_url="https://remote.example/inbox",
            follow_activity_id="https://remote.example/follows/1",
        )


    result = await create_thread(runtime, thread=build_thread(), starter_message=build_starter_message())

    from support.community_events import render_create
    canonical = render_create(runtime.fedify_gateway.render_community.await_args.args[0], user.actor_url).activity_json
    assert result.status == "published"
    with database.session() as session:
        source = session.scalar(
            select(UserActorOutboundSource).where(
                UserActorOutboundSource.source_kind == "content_create"
            )
        )
        delivery = session.scalar(select(UserActorOutboundDelivery))
        assert source is not None
        assert source.activity_id == canonical["id"]
        assert source.activity_json is not None
        import json

        assert json.loads(source.activity_json) == canonical
        assert delivery is not None
        assert delivery.target_endpoint_url == "https://remote.example/inbox"


@pytest.mark.asyncio
async def test_registered_user_thread_starter_uses_real_gateway_client_payload(
    tmp_path: Path,
) -> None:
    """A local-community starter must reach the real gateway client publish serializer."""

    database, runtime, requests = _real_gateway_runtime(tmp_path)
    _local_community(database)
    add_registered_user(database)

    try:
        result = await create_thread(runtime, thread=build_thread(), starter_message=build_starter_message())
    finally:
        await runtime.fedify_gateway.close()

    assert result.status == "published"
    assert len(requests) == 1
    request = requests[0]
    assert request['path'] == '/render-community'
    assert request['payload'] == {
        'actorUsername': 'alice',
        'communityActorUrl': 'https://bridge.example/actors/communities/hackers',
        'kind': 'post', 'title': 'Thread title', 'bodyMarkdown': 'hello from discord',
        'inReplyToObjectId': None,
        'operationId': source_ids(database)[0],
        'activityId': source_ids(database)[1], 'objectId': source_ids(database)[2],
        'publishedAt': '2026-10-20T00:00:00.000Z',
    }
