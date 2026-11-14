"""Scenario tests for deterministic community Create admission before network delivery."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.content_publish_service import ContentPublishService
from src.content_sync.persistence import PublicationConflictError
from src.db import Database
from src.models import (
    BridgeActorFollow,
    ChannelCommunitySubscription,
    MessageMapping,
    PublishedActivityObject,
    UserActorOutboundDelivery,
    UserActorOutboundSource,
)

ORIGIN = "https://bridge.example"
GROUP = "https://forum.example/category/42"
INBOX = "https://forum.example/category/42/inbox"
FOLLOW = f"{ORIGIN}/actors/services/bridge/activities/follow/group42"


class Policy:
    """Permit all hosts so these tests exercise durable publish admission."""

    def federation_decision(self, _address):
        """Return an allowed decision for one address."""
        return SimpleNamespace(allowed=True)


class FakeRenderer:
    """Render the requested IDs without performing any outbound HTTP delivery."""

    def __init__(self):
        """Record render-only invocations and forbid legacy send methods."""
        self.requests = []
        self.publish_content = AsyncMock(side_effect=AssertionError("Create sent before DB commit"))
        self.publish_local_community_content = AsyncMock(side_effect=AssertionError("unexpected local send"))

    async def render_community_create(self, request):
        """Return an exact canonical Create using the caller-supplied identity."""
        self.requests.append(request)
        kind = "Note" if request.software_profile == "nodebb" or request.kind == "comment" else "Page"
        actor = f"{ORIGIN}/actors/users/alice"
        public = "https://www.w3.org/ns/activitystreams#Public"
        object_json = {
            "id": request.object_id, "type": kind, "audience": GROUP,
            "attributedTo": actor, "to": [public, GROUP],
            "published": request.published_at,
            "source": {"content": request.body_markdown, "mediaType": "text/markdown"},
        }
        if request.kind == "post":
            object_json["name"] = request.title
        else:
            object_json["inReplyTo"] = request.in_reply_to_object_id
        if request.software_profile == "nodebb":
            object_json["context"] = request.conversation_root_object_id
        return SimpleNamespace(
            activity_id=request.activity_id,
            object_id=request.object_id,
            published_at=request.published_at,
            activity_json={
                "id": request.activity_id,
                "type": "Create",
                "actor": actor,
                "to": [public, GROUP], "cc": [actor, f"{actor}/followers"],
                "object": object_json,
            },
        )


class Wake:
    """Record worker notifications, not HTTP sends."""

    def __init__(self):
        """Initialize the wake counter."""
        self.count = 0

    def wake(self):
        """Record a new committed outbound source."""
        self.count += 1


def setup_database(tmp_path, *, profile="nodebb", followed=True):
    """Prepare one accepted channel with a checked Follow generation and local Person."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = Database(f"sqlite:///{tmp_path / 'community-create.sqlite'}")
    db.create_all()
    db.users.create_user(
        discord_user_id="123", activitypub_username="alice",
        actor_url=f"{ORIGIN}/actors/users/alice",
        inbox_url=f"{ORIGIN}/actors/users/alice/inbox",
        outbox_url=f"{ORIGIN}/actors/users/alice/outbox",
        followers_url=f"{ORIGIN}/actors/users/alice/followers",
        public_key_pem="public", private_key_pem="private",
    )
    with db.immediate_session() as session:
        session.add(ChannelCommunitySubscription(
            discord_channel_id=100, discord_guild_id=222,
            lemmy_community_actor_id=GROUP,
            community_inbox_url=INBOX, follow_activity_id=FOLLOW,
            status="accepted",
        ))
        if followed:
            session.add(BridgeActorFollow(
                community_actor_id=GROUP, community_inbox_url=INBOX,
                follow_activity_id=FOLLOW, remote_software_profile=profile,
                follow_dispatch_state="sent", cancel_requested=False, status="accepted",
            ))
    return db


def setup_action(db):
    """Construct the real service and two deterministic Discord event surfaces."""
    renderer, wake = FakeRenderer(), Wake()
    service = ContentPublishService(
        database=db, fedify_gateway=renderer,
        bridge_prefix="[bridge]", bridge_policy_service=Policy(),
        delivery_worker=wake,
    )
    created = datetime(2026, 11, 10, 12, 13, 14, tzinfo=timezone.utc)
    author = SimpleNamespace(id=123, display_name="Alice")
    starter = SimpleNamespace(
        id=301, guild_id=222, author=author, content="Hello", created_at=created,
        reply=AsyncMock(),
    )
    thread = SimpleNamespace(id=201, guild_id=222, parent_id=100, name="Title")
    comment = SimpleNamespace(
        id=302, guild_id=222, author=author, content="Response", created_at=created,
        channel=thread, reply=AsyncMock(),
    )
    return service, renderer, wake, thread, starter, comment


@pytest.mark.asyncio
@pytest.mark.parametrize("profile,expected_kind", [("nodebb", "Note"), ("threadiverse", "Page"), ("bridge", "Page")])
async def test_committed_remote_root_does_not_send_and_keeps_verified_direct_route(tmp_path, profile, expected_kind):
    """Accepted Person/root is committed with immutable Group and Follow before network."""
    db = setup_database(tmp_path, profile=profile)
    service, gateway, wake, thread, starter, _ = setup_action(db)
    result = await service.publish_thread_starter(thread=thread, starter_message=starter)
    assert result.status == "queued" and result.locally_accepted is True
    assert result.delivery_state == "queued"
    assert gateway.publish_content.await_count == 0 and wake.count == 1
    with db.session() as session:
        mapping = session.scalar(select(MessageMapping))
        object_row = session.scalar(select(PublishedActivityObject))
        source = session.scalar(select(UserActorOutboundSource))
        delivery = session.scalar(select(UserActorOutboundDelivery))
        assert mapping.object_id == result.object_id
        assert object_row.kind == "post"
        assert source.activity_id == result.activity_id
        snapshot = json.loads(source.snapshot_json)
        assert snapshot["directGroupRecipients"] == [{
            "actorId": GROUP, "inboxUrl": INBOX, "followActivityId": FOLLOW,
            "discordChannelId": 100, "discordGuildId": 222,
            "softwareProfile": profile,
        }]
        assert delivery.target_endpoint_url == INBOX
        assert json.loads(source.activity_json)["object"]["type"] == expected_kind


@pytest.mark.asyncio
async def test_duplicate_and_second_discord_message_have_stable_distinct_ids(tmp_path):
    """A retried Discord event reuses one source; a different event never collides."""
    db = setup_database(tmp_path)
    service, gateway, wake, thread, starter, _ = setup_action(db)
    first = await service.publish_thread_starter(thread=thread, starter_message=starter)
    second = await service.publish_thread_starter(thread=thread, starter_message=starter)
    assert second.activity_id == first.activity_id and second.object_id == first.object_id
    assert wake.count == 1 and len(gateway.requests) == 1
    starter.id = 309
    third = await service.publish_thread_starter(thread=thread, starter_message=starter)
    assert third.object_id != first.object_id and third.activity_id != first.activity_id
    assert wake.count == 2


@pytest.mark.asyncio
async def test_missing_or_ambiguous_follow_evidence_fails_closed_without_effects(tmp_path):
    """Unknown profile or missing accepted Follow cannot create a remote publication."""
    for profile, followed in [("unknown", True), ("nodebb", False)]:
        db = setup_database(tmp_path / f"{profile}-{followed}", profile=profile, followed=followed)
        service, gateway, wake, thread, starter, _ = setup_action(db)
        result = await service.publish_thread_starter(thread=thread, starter_message=starter)
        assert result.status == "rejected"
        assert not gateway.requests and wake.count == 0
        with db.session() as session:
            assert session.scalar(select(MessageMapping)) is None
        db.close()


@pytest.mark.asyncio
async def test_comment_uses_canonical_parent_and_stored_group_without_network(tmp_path):
    """One reply persists a distinct canonical Note and its parent before delivery."""
    db = setup_database(tmp_path)
    service, gateway, wake, thread, starter, comment = setup_action(db)
    root = await service.publish_thread_starter(thread=thread, starter_message=starter)
    reply = await service.publish_comment_to_community(
        message=comment, community_actor_url=GROUP,
        parent_object_id=root.object_id, publish_call=gateway.publish_content,
    )
    assert reply.status == "queued" and reply.activity_id != root.activity_id
    assert gateway.requests[-1].in_reply_to_object_id == root.object_id
    assert gateway.requests[-1].conversation_root_object_id == root.object_id
    assert gateway.publish_content.await_count == 0


@pytest.mark.asyncio
async def test_same_discord_source_cannot_be_reassigned_to_another_group(tmp_path):
    """Immutable Discord source/Person/Group identity rejects attempts to republish elsewhere."""
    db = setup_database(tmp_path)
    service, gateway, wake, thread, starter, _ = setup_action(db)
    await service.publish_thread_starter(thread=thread, starter_message=starter)
    with pytest.raises(PublicationConflictError):
        await service.publish_post_to_community(
            thread=thread, starter_message=starter,
            community_actor_url="https://foreign.example/category/99",
            publish_call=gateway.publish_content,
        )
    assert wake.count == 1


@pytest.mark.asyncio
async def test_follow_revoked_between_render_and_commit_has_no_outbox_effects(tmp_path):
    """A concurrent last-unsubscribe after render cannot admit an orphan direct route."""
    db = setup_database(tmp_path)
    service, gateway, wake, thread, starter, _ = setup_action(db)
    original_render = gateway.render_community_create

    async def revoke_after_render(request):
        """Model the Follow cancellation race at the gateway render boundary."""
        rendered = await original_render(request)
        with db.immediate_session() as session:
            follow = session.scalar(select(BridgeActorFollow))
            follow.cancel_requested = True
        return rendered

    gateway.render_community_create = revoke_after_render
    with pytest.raises(PublicationConflictError, match="no longer backed"):
        await service.publish_thread_starter(thread=thread, starter_message=starter)
    assert wake.count == 0 and gateway.publish_content.await_count == 0
    with db.session() as session:
        assert session.scalar(select(MessageMapping)) is None
        assert session.scalar(select(UserActorOutboundSource)) is None


@pytest.mark.asyncio
async def test_gateway_changed_canonical_object_is_not_committed(tmp_path):
    """An unexpected render-only object or type cannot enter the signing queue."""
    db = setup_database(tmp_path)
    service, gateway, wake, thread, starter, _ = setup_action(db)
    original_render = gateway.render_community_create

    async def corrupted_render(request):
        """Model a gateway response with an inconsistent canonical object type."""
        rendered = await original_render(request)
        rendered.activity_json["object"]["type"] = "Page"
        return rendered

    gateway.render_community_create = corrupted_render
    with pytest.raises(PublicationConflictError, match="gateway changed"):
        await service.publish_thread_starter(thread=thread, starter_message=starter)
    assert wake.count == 0 and gateway.publish_content.await_count == 0
    with db.session() as session:
        assert session.scalar(select(MessageMapping)) is None
        assert session.scalar(select(PublishedActivityObject)) is None


@pytest.mark.asyncio
async def test_followers_and_direct_group_share_one_target_delivery(tmp_path):
    """A Group endpoint already snapshotted for a User Actor follower is not duplicated."""
    db = setup_database(tmp_path)
    with db.immediate_session() as session:
        user = session.scalar(select(__import__("src.models", fromlist=["User"]).User))
        db.user_actor_followers.create_or_refresh(
            session, user_id=user.id, remote_actor_id="https://other.example/users/bob",
            remote_inbox_url=f"{INBOX}/personal", remote_shared_inbox_url=INBOX,
            follow_activity_id="https://other.example/follows/1",
        )
    service, gateway, wake, thread, starter, _ = setup_action(db)
    result = await service.publish_thread_starter(thread=thread, starter_message=starter)
    assert result.status == "queued"
    with db.session() as session:
        assert len(list(session.scalars(select(UserActorOutboundDelivery)))) == 1

@pytest.mark.asyncio
@pytest.mark.parametrize("profile,expected_type", [
    ("nodebb", "Note"), ("threadiverse", "Page"), ("bridge", "Page"),
])
async def test_dereference_uses_original_create_type_and_context(tmp_path, profile, expected_type):
    """A public GET must reconstruct the stored Create, not guess by post kind."""
    db = setup_database(tmp_path, profile=profile)
    try:
        service, _gateway, _wake, thread, starter, _ = setup_action(db)
        result = await service.publish_thread_starter(thread=thread, starter_message=starter)
        published = db.activitypub_objects.get_published_activity_object_by_object_id(result.object_id)
        assert published is not None
        object_type, context = db.activitypub_objects.get_original_create_object_shape(published)
        assert object_type == expected_type
        if profile == "nodebb":
            assert context == result.object_id
        else:
            assert context is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_dereference_rejects_source_with_different_object_identity(tmp_path):
    """A corrupted source cannot silently change the published AP object type."""
    db = setup_database(tmp_path)
    try:
        service, _gateway, _wake, thread, starter, _ = setup_action(db)
        result = await service.publish_thread_starter(thread=thread, starter_message=starter)
        with db.immediate_session() as session:
            source = session.scalar(select(UserActorOutboundSource))
            activity = json.loads(source.activity_json)
            activity["object"]["id"] = "https://different.example/object"
            source.activity_json = json.dumps(activity)
        published = db.activitypub_objects.get_published_activity_object_by_object_id(result.object_id)
        with pytest.raises(ValueError, match="Create identity conflicts"):
            db.activitypub_objects.get_original_create_object_shape(published)
    finally:
        db.close()

@pytest.mark.asyncio
async def test_internal_public_object_and_outbox_share_nodebb_create_shape(tmp_path):
    """Private read HTTP exposes the stored Note/context to both public routes."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.internal_fedify_api import create_internal_fedify_router

    db = setup_database(tmp_path)
    try:
        service, _gateway, _wake, thread, starter, _ = setup_action(db)
        result = await service.publish_thread_starter(thread=thread, starter_message=starter)
        runtime = SimpleNamespace(
            database=db,
            settings=SimpleNamespace(fedify_shared_secret="test-secret"),
        )
        app = FastAPI()
        app.include_router(create_internal_fedify_router(runtime))
        client = TestClient(app)
        headers = {"Authorization": "Bearer test-secret"}
        resolved = client.post(
            "/internal/fedify/published-objects/resolve",
            json={"object_id": result.object_id}, headers=headers,
        )
        assert resolved.status_code == 200
        assert resolved.json()["original_object_type"] == "Note"
        assert resolved.json()["original_object_context"] == result.object_id
        outbox = client.get(
            "/internal/fedify/actors/users/alice/outbox?page=1&page_size=40",
            headers=headers,
        )
        assert outbox.status_code == 200
        item = outbox.json()["items"][0]
        assert item["original_object_type"] == "Note"
        assert item["original_object_context"] == result.object_id
    finally:
        db.close()
