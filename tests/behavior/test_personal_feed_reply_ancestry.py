"""Behavioral regressions for local and remote Personal Feed reply ancestry."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import discord
import pytest

from src.db import Database
from src.fedify_gateway_client import RenderPersonalFeedResult, ResolvedRemotePerson
from src.models import InboundDiscordDelivery, PublishedActivityObject, User
from src.personal_feed_conversations import PersonalFeedConversationRuntime
from src.personal_feed_discord_delivery_worker import PersonalFeedDiscordDeliveryWorker
from src.personal_feeds import PersonalFeedService

ROOT = "https://bridge.example/o/root"
REMOTE_ACTOR = "https://remote.example/users/bob"


class Gateway:
    """Record the real runtime's render requests at the external HTTP boundary."""

    def __init__(self) -> None:
        self.requests: list[object] = []

    async def resolve_remote_person(self, actor_id: str) -> ResolvedRemotePerson:
        """Return a canonical remote Person and its valid inbox at the HTTP edge."""
        return ResolvedRemotePerson(actor_id, f"{actor_id}/inbox")

    async def render_personal_feed(self, request: object) -> RenderPersonalFeedResult:
        """Provide an otherwise valid canonical gateway Create response."""
        self.requests.append(request)
        actor_url = "https://bridge.example/actors/users/charlie"
        ids = request.direct_person_actor_ids
        activity = {
            "cc": [actor_url, f"{actor_url}/followers", *ids],
            "id": request.activity_id,
            "type": "Create",
            "actor": "https://bridge.example/actors/users/charlie",
            "object": {
                "id": request.object_id,
                "type": "Note",
                "attributedTo": "https://bridge.example/actors/users/charlie",
                "cc": [f"{actor_url}/followers", *ids],
                "inReplyTo": request.in_reply_to_object_id,
                "content": request.body_markdown,
            },
        }
        return RenderPersonalFeedResult(
            request.activity_id, request.object_id, request.published_at, activity
        )


def environment(tmp_path):
    """Build a migrated DB, active Personal Feed, and registered reply author."""
    database = Database(f"sqlite:///{tmp_path / 'conversation.sqlite3'}")
    database.migrate()
    with database.session() as session:
        for user_id, username in (("10", "alice"), ("20", "charlie")):
            actor = f"https://bridge.example/actors/users/{username}"
            session.add(
                User(
                    discord_user_id=user_id,
                    activitypub_username=username,
                    actor_url=actor,
                    inbox_url=f"{actor}/inbox",
                    outbox_url=f"{actor}/outbox",
                    followers_url=f"{actor}/followers",
                    public_key_pem="public",
                    private_key_pem="private",
                )
            )
        session.add(
            PublishedActivityObject(
                actor_username="alice",
                actor_url="https://bridge.example/actors/users/alice",
                publication_context="personal_feed",
                community_actor_url=None,
                activity_id="https://bridge.example/a/root",
                object_id=ROOT,
                kind="post",
                title=None,
                body_markdown="root",
                in_reply_to_object_id=None,
                conversation_root_object_id=ROOT,
                discord_guild_id=1,
                discord_channel_id=10,
                discord_message_id=100,
            )
        )
    service = PersonalFeedService(database)
    service.bind_or_move("10", 1, 10)
    database.personal_feed_conversations.adopt_thread(
        root_object_id=ROOT, channel_id=10, root_message_id=100, thread_id=100
    )
    gateway = Gateway()
    runtime = PersonalFeedConversationRuntime(
        database=database,
        personal_feed_service=service,
        fedify_gateway=gateway,
        user_ban_service=SimpleNamespace(
            check_global_discord_user=lambda _id: SimpleNamespace(banned=False)
        ),
        bridge_policy_service=SimpleNamespace(
            is_discord_guild_allowed=lambda _id: True,
            federation_decision=lambda _url: SimpleNamespace(allowed=True),
        ),
        fedify_origin="https://bridge.example",
    )
    return database, service, gateway, runtime


def comment(database, *, obj_id: str, parent: str = ROOT, msg_id=None):
    """Admit one real remote Create and optionally acknowledge Discord delivery."""
    result = database.personal_feed_conversations.admit_create(
        activity_id=f"{obj_id}/create",
        object_id=obj_id,
        root_object_id=ROOT,
        parent_object_id=parent,
        activity_actor_id=REMOTE_ACTOR,
        attributed_to_actor_id=REMOTE_ACTOR,
        actor_name="Bob",
        content_markdown="a remote reply",
        published_at=datetime.now(timezone.utc),
        source_activity={"type": "Create"},
        destination_channel_id=10,
        parent_known=True,
    )
    if msg_id is not None:
        database.personal_feed_conversations.set_delivery_thread(result.delivery_id, 100)
        database.personal_feed_conversations.record_discord_attempt(
            result.delivery_id, success=True, discord_message_id=msg_id
        )
    return result


def local_message(*, msg_id=502, parent_id=501):
    """Represent one registered author's native Discord Reply in the root thread."""
    return SimpleNamespace(
        id=msg_id,
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=100),
        author=SimpleNamespace(id=20),
        content="reply to Bob",
        created_at=datetime.now(timezone.utc),
        reference=SimpleNamespace(message_id=parent_id),
    )


@pytest.mark.asyncio
async def test_local_discord_reply_to_remote_comment_preserves_immediate_parent(tmp_path):
    """Charlie replies to Bob's bot comment, not just the personal post root."""
    database, _service, gateway, runtime = environment(tmp_path)
    bob = "https://remote.example/o/bob"
    comment(database, obj_id=bob, msg_id=501)

    result = await runtime.publish_local_reply(message=local_message())

    assert result.status == "published"
    assert gateway.requests[0].in_reply_to_object_id == bob
    with database.session() as session:
        published = session.query(PublishedActivityObject).filter_by(kind="comment").one()
        assert published.in_reply_to_object_id == bob
        assert published.conversation_root_object_id == ROOT


@pytest.mark.asyncio
async def test_unknown_or_foreign_discord_reference_does_not_steal_parent(tmp_path):
    """A foreign root's message or unknown snowflake resolves only to this root."""
    _db, _service, gateway, runtime = environment(tmp_path)
    for msg_id, referenced in ((503, 99999), (504, 100)):
        result = await runtime.publish_local_reply(
            message=local_message(msg_id=msg_id, parent_id=referenced)
        )
        assert result.status == "published"
        assert result.reason == ("published" if referenced == 100 else "fallback_unknown_reference")
        assert gateway.requests[-1].in_reply_to_object_id == ROOT


@pytest.mark.asyncio
async def test_incoming_nested_comment_is_sent_as_native_reply(tmp_path):
    """A child of an already delivered remote comment replies to that exact message."""
    database, service, _gateway, _runtime = environment(tmp_path)
    parent = "https://remote.example/o/bob"
    comment(database, obj_id=parent, msg_id=501)
    child = comment(database, obj_id="https://remote.example/o/child", parent=parent)
    sent = []

    async def send(_body, **kwargs):
        sent.append(kwargs)
        return SimpleNamespace(id=700)

    thread = SimpleNamespace(
        id=100,
        guild=SimpleNamespace(me=object()),
        permissions_for=lambda _me: SimpleNamespace(read_message_history=True),
        send=send,
    )
    worker = PersonalFeedDiscordDeliveryWorker(
        database=database,
        bot=SimpleNamespace(get_channel=lambda _id: thread),
        personal_feed_service=service,
    )

    claims = database.personal_feed_conversations.claim_due()
    await worker._deliver(next(c for c in claims if c.delivery_id == child.delivery_id))

    assert len(sent) == 1
    reference = sent[0]["reference"]
    assert reference.message_id == 501
    assert reference.channel_id == 100
    assert reference.fail_if_not_exists is True
    with database.session() as session:
        assert session.get(InboundDiscordDelivery, child.delivery_id).discord_message_id == 700


@pytest.mark.asyncio
async def test_missing_parent_or_permission_denial_never_records_flattened_success(tmp_path):
    """Deleted references and missing history permission fail as per-target attempts."""
    database, service, _gateway, _runtime = environment(tmp_path)
    parent = "https://remote.example/o/bob"
    comment(database, obj_id=parent, msg_id=501)
    child = comment(database, obj_id="https://remote.example/o/child", parent=parent)
    delivered = []

    async def send(_body, **kwargs):
        delivered.append(kwargs)
        raise RuntimeError("referenced message deleted just before send")

    guild = SimpleNamespace(me=object())
    thread = SimpleNamespace(
        id=100,
        guild=guild,
        permissions_for=lambda _me: SimpleNamespace(read_message_history=True),
        send=send,
    )
    worker = PersonalFeedDiscordDeliveryWorker(
        database=database,
        bot=SimpleNamespace(get_channel=lambda _id: thread),
        personal_feed_service=service,
    )
    claim = next(c for c in database.personal_feed_conversations.claim_due() if c.delivery_id == child.delivery_id)
    await worker._deliver(claim)
    assert delivered[0]["reference"].fail_if_not_exists is True
    with database.session() as session:
        row = session.get(InboundDiscordDelivery, child.delivery_id)
        assert row.discord_message_id is None
        assert row.attempt_count == 1
        # Force the retry due, then remove the permission.
        row.next_attempt_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    thread.permissions_for = lambda _me: SimpleNamespace(read_message_history=False)
    claim = next(c for c in database.personal_feed_conversations.claim_due() if c.delivery_id == child.delivery_id)
    await worker._deliver(claim)
    assert len(delivered) == 1
    with database.session() as session:
        row = session.get(InboundDiscordDelivery, child.delivery_id)
        assert row.discord_message_id is None
        assert row.attempt_count == 2


@pytest.mark.asyncio
async def test_child_waits_for_discord_parent_and_resumes_after_success(tmp_path):
    """Out-of-order remote children wait until their parent has a Discord placement."""
    database, service, _gateway, _runtime = environment(tmp_path)
    parent = "https://remote.example/o/bob"
    child = comment(database, obj_id="https://remote.example/o/child", parent=parent)
    comment(database, obj_id=parent)
    attempted = []

    async def send(_body, **kwargs):
        attempted.append(kwargs)
        return SimpleNamespace(id=700 + len(attempted))

    thread = SimpleNamespace(id=100, send=send)
    worker = PersonalFeedDiscordDeliveryWorker(
        database=database,
        bot=SimpleNamespace(get_channel=lambda _id: thread),
        personal_feed_service=service,
    )
    claims = database.personal_feed_conversations.claim_due()
    claim = next(c for c in claims if c.delivery_id == child.delivery_id)
    await worker._deliver(claim)
    assert attempted == []
    with database.session() as session:
        assert session.get(InboundDiscordDelivery, child.delivery_id).state == "pending_parent"
    # Deliver the parent through the worker, which must wake the child.
    parent_claim = next(c for c in claims if c.object_id == parent)
    await worker._deliver(parent_claim)
    with database.session() as session:
        assert session.get(InboundDiscordDelivery, child.delivery_id).state == "ready"
    child_claim = next(c for c in database.personal_feed_conversations.claim_due() if c.delivery_id == child.delivery_id)
    await worker._deliver(child_claim)
    assert len(attempted) == 2
    assert attempted[0].get("reference") is None
    assert attempted[1]["reference"].message_id == 701



@pytest.mark.asyncio
async def test_local_reply_to_local_comment_preserves_immediate_parent(tmp_path):
    """Two registered Discord replies retain the first local comment AP identity."""
    _database, _service, gateway, runtime = environment(tmp_path)
    first = await runtime.publish_local_reply(message=local_message(msg_id=502, parent_id=100))
    second = await runtime.publish_local_reply(message=local_message(msg_id=503, parent_id=502))
    assert first.status == second.status == "published"
    assert gateway.requests[-1].in_reply_to_object_id == first.object_id


@pytest.mark.asyncio
async def test_known_foreign_root_comment_cannot_be_selected_by_discord_snowflake(tmp_path):
    """A valid mapping belonging to another root is not a valid parent here."""
    from src.models import MessageMapping

    database, _service, gateway, runtime = environment(tmp_path)
    with database.session() as session:
        session.add(
            PublishedActivityObject(
                actor_username="alice", actor_url="https://bridge.example/actors/users/alice",
                publication_context="personal_feed", community_actor_url=None,
                activity_id="https://bridge.example/a/foreign", object_id="https://bridge.example/o/foreign",
                kind="comment", title=None, body_markdown="foreign",
                in_reply_to_object_id="https://bridge.example/o/different-root",
                conversation_root_object_id="https://bridge.example/o/different-root",
                discord_guild_id=1, discord_channel_id=11, discord_message_id=802,
            )
        )
        session.add(
            MessageMapping(
                source_platform="discord", source_id="802",
                activity_id="https://bridge.example/a/foreign", object_id="https://bridge.example/o/foreign",
                actor_url="https://bridge.example/actors/users/alice",
                publication_context="personal_feed", community_actor_url=None,
                discord_guild_id=1, discord_channel_id=11, discord_message_id=802,
            )
        )
    result = await runtime.publish_local_reply(message=local_message(msg_id=803, parent_id=802))
    assert result.status == "published"
    assert gateway.requests[-1].in_reply_to_object_id == ROOT


def test_orphan_child_has_bounded_terminal_failure_without_discord_send(tmp_path):
    """After a week without its parent, an orphan keeps a durable failed state."""
    database, _service, _gateway, _runtime = environment(tmp_path)
    child = comment(database, obj_id="https://remote.example/o/orphan", parent="https://remote.example/o/missing")
    with database.session() as session:
        row = session.get(InboundDiscordDelivery, child.delivery_id)
        row.state = "pending_parent"
        row.next_attempt_at = None
        row.created_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    assert database.personal_feed_conversations.claim_due() == []
    with database.session() as session:
        row = session.get(InboundDiscordDelivery, child.delivery_id)
        assert row.state == "failed"
        assert "parent unavailable" in row.last_error


def test_federated_child_with_known_foreign_parent_is_rejected_at_inbox_boundary(tmp_path):
    """A comment rooted elsewhere cannot borrow this Personal Feed thread."""
    database, _service, _gateway, runtime = environment(tmp_path)
    with database.session() as session:
        session.add(
            PublishedActivityObject(
                actor_username="alice",
                actor_url="https://bridge.example/actors/users/alice",
                publication_context="personal_feed", community_actor_url=None,
                activity_id="https://bridge.example/a/other", object_id="https://bridge.example/o/other",
                kind="comment", title=None, body_markdown="other",
                in_reply_to_object_id="https://bridge.example/o/unrelated",
                conversation_root_object_id="https://bridge.example/o/unrelated",
                discord_guild_id=1, discord_channel_id=11, discord_message_id=900,
            )
        )
    event = SimpleNamespace(
        event_type="comment.created", actor_id=REMOTE_ACTOR,
        target=SimpleNamespace(root_object_id=ROOT),
        object=SimpleNamespace(
            ap_id="https://remote.example/o/child-of-other",
            parent_ap_id="https://bridge.example/o/other",
            attributed_to_actor_id=REMOTE_ACTOR,
            author_name="Bob", body_markdown="invalid context", published_at=None,
        ),
        delivery_id="https://remote.example/a/child-of-other",
    )
    result = runtime.handle_inbound_event(event)
    assert (result.status, result.reason) == ("rejected", "foreign_parent_root")
    assert database.personal_feed_conversations.get_inbound_object(
        "https://remote.example/o/child-of-other"
    ) is None
