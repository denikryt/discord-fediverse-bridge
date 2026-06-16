"""Stage 5 post/comment lifecycle coverage through real bridge runtimes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from src.community_sync.runtime import CommunityRuntime
from src.content_publish_service import ContentPublishService
from src.fedify_gateway_client import (
    CanonicalMutationResult,
    PublishContentResult,
    PublishLocalCommunityContentResult,
)
from src.local_communities.runtime import LocalCommunityRuntime
from src.local_communities.service import LocalCommunityService
from src.models import PublishedActivityObject, UserActorOutboundSource
from src.user_actor_delivery import UserActorDeliveryService
from support.db import (
    add_accepted_subscription,
    add_registered_user,
    build_database,
    create_source_thread_group,
)
from support.discord import build_starter_message, build_thread, build_thread_message
from support.runtime import build_test_policy_service


class _MutationGateway:
    def __init__(self, create_results: list[object], actor_url: str) -> None:
        self.publish_content = AsyncMock()
        self.publish_local_community_content = AsyncMock(side_effect=create_results)
        if create_results:
            self.publish_content.return_value = create_results[-1]
        self.actor_url = actor_url
        self.update_requests: list[object] = []
        self.delete_requests: list[object] = []

    async def update_content(self, request):
        self.update_requests.append(request)
        return CanonicalMutationResult(
            activity_id=request.activity_id,
            object_id=request.ap_object_id,
            activity_json={
                "id": request.activity_id,
                "type": "Update",
                "actor": self.actor_url,
                "to": ["https://www.w3.org/ns/activitystreams#Public"],
                "cc": [f"{self.actor_url}/followers"],
                "object": {
                    "id": request.ap_object_id,
                    "type": "Note" if request.kind == "comment" else "Page",
                    "attributedTo": self.actor_url,
                    "audience": request.community_actor_url,
                    "inReplyTo": request.in_reply_to_object_id,
                    "content": request.body_markdown,
                },
            },
        )

    async def delete_content(self, request):
        self.delete_requests.append(request)
        return CanonicalMutationResult(
            activity_id=request.activity_id,
            object_id=request.ap_object_id,
            activity_json={
                "id": request.activity_id,
                "type": "Delete",
                "actor": self.actor_url,
                "to": ["https://www.w3.org/ns/activitystreams#Public"],
                "cc": [f"{self.actor_url}/followers"],
                "object": request.ap_object_id,
            },
        )

    async def send_user_actor_deliveries(self, **_kwargs):
        return []


def _follow(database, user_id: int) -> None:
    with database.immediate_session() as session:
        database.user_actor_followers.create_or_refresh(
            session,
            user_id=user_id,
            remote_actor_id="https://remote.example/users/follower",
            remote_inbox_url="https://remote.example/users/follower/inbox",
            remote_shared_inbox_url="https://remote.example/inbox",
            follow_activity_id="https://remote.example/follows/1",
        )


def _runtime_namespace(policy, service):
    return SimpleNamespace(
        bridge_policy_service=policy,
        user_actor_delivery_service=service,
        user_actor_delivery_worker=SimpleNamespace(wake=Mock()),
    )


@pytest.mark.asyncio
async def test_remote_comment_create_update_delete_uses_real_runtime_and_one_canonical_lifecycle(
    tmp_path: Path,
) -> None:
    database = build_database(tmp_path, "remote-comment-lifecycle.db")
    add_registered_user(database)
    add_accepted_subscription(database, channel_id=100)
    user = database.users.get_user_by_discord_user_id("123")
    assert user is not None
    _follow(database, user.id)
    create_source_thread_group(
        database,
        channel_id=100,
        thread_id=200,
        starter_message_id=300,
        ap_object_id="https://remote.example/post/root",
        ap_activity_id="https://remote.example/activities/create/root",
    )
    canonical_create = {
        "id": "https://bridge.example/users/alice/activities/create/comment/1",
        "type": "Create",
        "actor": user.actor_url,
        "to": ["https://www.w3.org/ns/activitystreams#Public"],
        "cc": [user.followers_url],
        "object": {
            "id": "https://bridge.example/users/alice/comment/1",
            "type": "Note",
            "attributedTo": user.actor_url,
            "audience": "https://lemmy.example/c/hackers",
            "inReplyTo": "https://remote.example/post/root",
        },
    }
    gateway = _MutationGateway([], user.actor_url)
    gateway.publish_content.return_value = PublishContentResult(
        activity_id=canonical_create["id"],
        object_id=canonical_create["object"]["id"],
        community_actor_url="https://lemmy.example/c/hackers",
        activity_json=canonical_create,
    )
    policy = build_test_policy_service(database)
    publish_service = ContentPublishService(
        database=database,
        fedify_gateway=gateway,
        bridge_prefix="[bridge]",
        bridge_policy_service=policy,
    )
    community_runtime = CommunityRuntime(
        database=database, content_publish_service=publish_service
    )
    service = UserActorDeliveryService(database, gateway, policy)

    created = await community_runtime.handle_discord_message(
        build_thread_message(message_id=301, reference_message_id=None)
    )
    assert created.status == "published"
    await community_runtime.handle_discord_message_edit(
        message_id=301,
        new_content="edited remote comment",
        author_display_name="Alice",
        runtime=_runtime_namespace(policy, service),
        edited_timestamp=datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc),
    )
    await community_runtime.handle_discord_message_delete(
        message_id=301, runtime=_runtime_namespace(policy, service)
    )

    with database.session() as session:
        published = session.scalar(
            select(PublishedActivityObject).where(
                PublishedActivityObject.object_id == canonical_create["object"]["id"]
            )
        )
        sources = list(
            session.scalars(
                select(UserActorOutboundSource)
                .where(UserActorOutboundSource.published_object_id == published.id)
                .order_by(UserActorOutboundSource.logical_version)
            )
        )
        assert [source.source_kind for source in sources] == [
            "content_create",
            "content_update",
            "content_delete",
        ]
        assert json.loads(sources[0].activity_json) == canonical_create
        update = json.loads(sources[1].activity_json)
        assert update["object"]["inReplyTo"] == "https://remote.example/post/root"
        assert update["object"]["attributedTo"] == user.actor_url
        assert json.loads(sources[2].activity_json)["object"] == published.object_id


@pytest.mark.asyncio
async def test_local_comment_create_update_delete_preserves_user_create_and_community_context(
    tmp_path: Path,
) -> None:
    database = build_database(tmp_path, "local-comment-lifecycle.db")
    add_registered_user(database)
    user = database.users.get_user_by_discord_user_id("123")
    assert user is not None
    _follow(database, user.id)
    LocalCommunityService(
        database=database,
        base_url="https://bridge.example",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=100,
        slug="hackers",
        name="Hackers",
        description="Local",
        created_by_discord_user_id="123",
    )
    community = "https://bridge.example/communities/hackers"
    root_create = {
        "id": "https://bridge.example/users/alice/activities/create/post/1",
        "type": "Create",
        "actor": user.actor_url,
        "object": {"id": "https://bridge.example/users/alice/post/1", "type": "Page", "attributedTo": user.actor_url, "audience": community},
    }
    comment_create = {
        "id": "https://bridge.example/users/alice/activities/create/comment/1",
        "type": "Create",
        "actor": user.actor_url,
        "object": {"id": "https://bridge.example/users/alice/comment/1", "type": "Note", "attributedTo": user.actor_url, "audience": community, "inReplyTo": root_create["object"]["id"]},
    }
    gateway = _MutationGateway(
        [
            PublishLocalCommunityContentResult(root_create["id"], root_create["object"]["id"], community, 1, 0, root_create),
            PublishLocalCommunityContentResult(comment_create["id"], comment_create["object"]["id"], community, 1, 0, comment_create),
        ],
        user.actor_url,
    )
    policy = build_test_policy_service(database)
    publish_service = ContentPublishService(database=database, fedify_gateway=gateway, bridge_prefix="[bridge]", bridge_policy_service=policy)
    local_runtime = LocalCommunityRuntime(database=database, fedify_gateway=gateway, content_publish_service=publish_service, bridge_prefix="[bridge]", bridge_policy_service=policy)
    service = UserActorDeliveryService(database, gateway, policy)
    runtime_ns = _runtime_namespace(policy, service)

    await local_runtime.handle_discord_thread_create(thread=build_thread(), starter_message=build_starter_message())
    result = await local_runtime.handle_discord_message(message=build_thread_message())
    assert result.status == "published"
    await local_runtime.handle_discord_message_edit(
        message_id=301,
        new_content="edited local comment",
        runtime=runtime_ns,
        edited_timestamp=datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc),
    )
    await local_runtime.handle_discord_message_delete(message_id=301, runtime=runtime_ns)

    with database.session() as session:
        published = session.scalar(select(PublishedActivityObject).where(PublishedActivityObject.object_id == comment_create["object"]["id"]))
        sources = list(session.scalars(select(UserActorOutboundSource).where(UserActorOutboundSource.published_object_id == published.id).order_by(UserActorOutboundSource.logical_version)))
        assert [source.source_kind for source in sources] == ["content_create", "content_update", "content_delete"]
        assert json.loads(sources[0].activity_json) == comment_create
        update = json.loads(sources[1].activity_json)
        assert update["object"]["audience"] == community
        assert update["object"]["inReplyTo"] == root_create["object"]["id"]
        assert json.loads(sources[2].activity_json)["actor"] == user.actor_url
