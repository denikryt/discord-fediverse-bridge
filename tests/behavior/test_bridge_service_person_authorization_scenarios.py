"""Signed Bridge Person content may use only its accepted Service relationship.

The HTTP-signature verification is the Fedify boundary. These scenarios enter
with the gateway-derived outer sender and exercise the real subscriber database,
HTTPS actor admission, and local host handler before Discord is contacted.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.activitypub_models import ActivityPubEvent
from src.bridge_policy import BridgePolicyService
from src.db import Database
from src.inbound_activity_outcomes import InboundActivityOutcome
from src.local_communities.actor_authorization import authorize_inbound_source
from src.local_communities.inbound_create import LocalCommunityInboundCreateHandler

HOST = "https://bridge.example/actors/communities/forum"
ORIGIN = "https://remote.example"
SERVICE = f"{ORIGIN}/actors/services/bridge"
PERSON = f"{ORIGIN}/actors/users/alice"
OBJECT = f"{ORIGIN}/actors/users/alice/objects/posts/7"


def _db(tmp_path):
    """Create a real tenant and one accepted remote Bridge Service subscriber."""
    db = Database(f"sqlite:///{tmp_path / 'cp20.db'}")
    db.create_all()
    community = db.local_communities.create_local_community(
        discord_guild_id=10, discord_forum_channel_id=11,
        slug="forum", display_name="Forum", summary=None,
        created_by_discord_user_id="12", actor_url=HOST,
        inbox_url=f"{HOST}/inbox", outbox_url=f"{HOST}/outbox",
        followers_url=f"{HOST}/followers", public_key_pem="pub", private_key_pem="priv",
    )
    service = db.remote_subscribers.create_remote_subscriber(
        local_community_id=community.id, remote_actor_id=SERVICE,
        remote_inbox_url=f"{SERVICE}/inbox", follow_activity_id=f"{ORIGIN}/follow/1",
    )
    policy = BridgePolicyService(settings=SimpleNamespace(
        federation_allowlist=[], federation_blocklist=[],
        discord_guild_allowlist=[], discord_guild_blocklist=[],
        bridge_super_admin_user_ids=[],
    ), repository=db.bridge_policy_entries)
    return db, community, service, policy


def _event(*, author=PERSON, verified=PERSON, wrapped=False, owner=PERSON):
    """Represent one signed Create with immutable outer and object identity."""
    return ActivityPubEvent.model_validate({
        "event_type": "post.created", "delivery_id": f"{ORIGIN}/activities/create/7",
        "occurred_at": "2026-11-12T01:00:00Z",
        "community_actor_id": HOST, "verified_destination_group_actor_id": HOST,
        "actor_id": author, "verified_sender_actor_id": verified,
        "source_announce_id": f"{ORIGIN}/announces/7" if wrapped else None,
        "source_activity_json": {"type": "Create", "actor": author,
            "object": {"type": "Page", "id": OBJECT, "attributedTo": owner}},
        "object": {
            "ap_id": OBJECT, "kind": "post", "lemmy_id": 0,
            "published_at": "2026-11-12T01:00:00Z", "author_name": "alice",
            "body_markdown": "hello", "url": OBJECT, "attributed_to_actor_id": owner,
        },
    })


def _actor(actor_id, kind, *, declaration=False):
    """Model the public actor fields produced by the Bridge Fedify dispatcher."""
    result = {
        "type": kind, "id": actor_id,
        "inbox": actor_id + "/inbox", "outbox": actor_id + "/outbox",
        "endpoints": {"sharedInbox": ORIGIN + "/inbox"},
        "publicKey": {"id": actor_id + "#main-key", "owner": actor_id,
                      "publicKeyPem": "-----BEGIN PUBLIC KEY-----..."},
    }
    if declaration:
        result["attachment"] = [{"type": "PropertyValue", "name": "Bridge software",
                                 "value": "discord-fediverse-bridge"}]
    return result


def _fetch(*, person=None, service=None, on_fetch=None):
    """Only outer network transport is simulated, never the subscriber DB."""
    rows = {PERSON: person or _actor(PERSON, "Person"),
            SERVICE: service or _actor(SERVICE, "Service", declaration=True)}
    async def fetch(actor_id, _accept):
        if on_fetch is not None:
            on_fetch(actor_id)
        return rows[actor_id]
    return fetch


@pytest.mark.asyncio
async def test_signed_registered_person_uses_only_its_accepted_bridge_service(tmp_path):
    """A signed public Person with matching Service metadata resolves the origin."""
    db, community, _, policy = _db(tmp_path)
    result = await authorize_inbound_source(
        event=_event(), local_community_id=community.id,
        database=db, policy=policy, fetch_actor=_fetch(),
    )
    assert result is not None
    assert result.author_actor_id == PERSON
    assert result.subscriber_actor_id == SERVICE
    assert result.evidence_kind == "bridge_service_person"
    assert len(db.remote_subscribers.list_remote_subscribers(community.id, status="accepted")) == 1
    db.close()


@pytest.mark.asyncio
async def test_same_actor_subscriber_requires_no_bridge_metadata_or_remote_fetch(tmp_path):
    """An existing same-actor accepted follower keeps its legacy permissions."""
    db, community, _, policy = _db(tmp_path)
    db.remote_subscribers.create_remote_subscriber(
        local_community_id=community.id, remote_actor_id=PERSON,
        remote_inbox_url=PERSON + "/inbox", follow_activity_id=ORIGIN + "/follow/2",
    )
    async def unavailable(*_args):
        raise AssertionError("same-actor permission must not fetch another actor")
    result = await authorize_inbound_source(
        event=_event(), local_community_id=community.id,
        database=db, policy=policy, fetch_actor=unavailable,
    )
    assert result is not None and result.subscriber_actor_id == PERSON
    # Existing same-actor access never authorizes a claimed foreign owner.
    denied = await authorize_inbound_source(
        event=_event(owner=SERVICE), local_community_id=community.id,
        database=db, policy=policy, fetch_actor=unavailable,
    )
    assert denied is None
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", [
    "bad_sender", "wrapped", "bad_owner", "missing_marker", "wrong_person_type",
    "wrong_key_owner", "wrong_service_id", "wrong_inbox", "wrong_namespace",
])
async def test_spoofed_or_nonbridge_person_has_no_subscriber_rights(tmp_path, variant):
    """Each independent proof failure rejects the Create before side effects."""
    db, community, _, policy = _db(tmp_path)
    event = _event()
    person = _actor(PERSON, "Person")
    service = _actor(SERVICE, "Service", declaration=True)
    if variant == "bad_sender": event = _event(verified=SERVICE)
    if variant == "wrapped": event = _event(wrapped=True)
    if variant == "bad_owner": event = _event(owner=f"{ORIGIN}/actors/users/attacker")
    if variant == "missing_marker": service.pop("attachment")
    if variant == "wrong_person_type": person["type"] = "Application"
    if variant == "wrong_key_owner": person["publicKey"]["owner"] = SERVICE
    if variant == "wrong_service_id": service["id"] = f"{ORIGIN}/actors/services/evil"
    if variant == "wrong_inbox": service["inbox"] = f"{ORIGIN}/other/inbox"
    if variant == "wrong_namespace": event = _event(author=f"{ORIGIN}/users/alice", verified=f"{ORIGIN}/users/alice")
    result = await authorize_inbound_source(
        event=event, local_community_id=community.id,
        database=db, policy=policy, fetch_actor=_fetch(person=person, service=service),
    )
    assert result is None
    assert len(db.remote_subscribers.list_remote_subscribers(community.id, status="accepted")) == 1
    db.close()


@pytest.mark.asyncio
async def test_undo_during_actor_fetch_rejects_stale_service_and_no_new_row(tmp_path):
    """Revoking the subscriber while HTTP is pending invalidates the result."""
    db, community, _, policy = _db(tmp_path)
    def undo(actor_id):
        if actor_id == SERVICE:
            db.remote_subscribers.delete_accepted_if_current_follow(
                local_community_id=community.id, remote_actor_id=SERVICE,
                follow_activity_id=f"{ORIGIN}/follow/1",
            )
    result = await authorize_inbound_source(
        event=_event(), local_community_id=community.id,
        database=db, policy=policy, fetch_actor=_fetch(on_fetch=undo),
    )
    assert result is None
    assert db.remote_subscribers.get_remote_subscriber(
        local_community_id=community.id, remote_actor_id=SERVICE) is None
    db.close()


@pytest.mark.asyncio
async def test_unauthorized_duplicate_cannot_fanout_or_lookup_host(tmp_path):
    """A forged duplicate is refused before host/relay/parent paths are used."""
    db, community, _, policy = _db(tmp_path)
    host = AsyncMock()
    siblings = AsyncMock()
    relay = AsyncMock()
    handler = LocalCommunityInboundCreateHandler(
        database=db, surface_fanout=siblings, federation_fanout=relay,
        discord_client_provider=lambda: host, bridge_policy_service=policy,
        actor_fetcher=_fetch(),
    )
    result = await handler.handle_post(_event(verified=SERVICE), runtime=None)
    assert result.status == "skipped"
    assert result.outcome is InboundActivityOutcome.IGNORED_ACTOR_NOT_SUBSCRIBER
    host.wait_until_bridge_ready.assert_not_awaited()
    siblings.fanout_thread_to_local_subscribers.assert_not_awaited()
    relay.relay_create.assert_not_awaited()
    assert db.local_community_content.get_local_community_thread_by_ap_object_id(OBJECT) is None
    db.close()


@pytest.mark.asyncio
async def test_signed_bridge_person_creates_one_host_thread_for_its_tenant(tmp_path):
    """A valid Service-subscribed Person reaches the host once with DB mapping."""
    db, community, _, policy = _db(tmp_path)
    created = SimpleNamespace(id=801)
    starter = SimpleNamespace(id=802)
    forum = SimpleNamespace(create_thread=AsyncMock(return_value=(created, starter)))
    bot = SimpleNamespace(
        wait_until_bridge_ready=AsyncMock(),
        fetch_forum_channel=AsyncMock(return_value=forum),
    )
    siblings = AsyncMock()
    relay = AsyncMock()
    handler = LocalCommunityInboundCreateHandler(
        database=db, surface_fanout=siblings, federation_fanout=relay,
        discord_client_provider=lambda: bot, bridge_policy_service=policy,
        actor_fetcher=_fetch(),
    )
    result = await handler.handle_post(_event(), runtime=None)
    assert result.status == "processed"
    row = db.local_community_content.get_local_community_thread_by_ap_object_id(OBJECT)
    assert row is not None and row.local_community_id == community.id
    forum.create_thread.assert_awaited_once()
    siblings.fanout_thread_to_local_subscribers.assert_awaited_once()
    relay.relay_create.assert_awaited_once()
    # Forged repeated Create cannot reach either fanout, even with a host row.
    invalid = await handler.handle_post(_event(verified=SERVICE), runtime=None)
    assert invalid.outcome is InboundActivityOutcome.IGNORED_ACTOR_NOT_SUBSCRIBER
    forum.create_thread.assert_awaited_once()
    relay.relay_create.assert_awaited_once()
    db.close()


@pytest.mark.asyncio
async def test_a_second_service_on_same_origin_is_not_arbitrarily_selected(tmp_path):
    """An ambiguous same-origin subscriber set cannot confer Person access."""
    db, community, _, policy = _db(tmp_path)
    db.remote_subscribers.create_remote_subscriber(
        local_community_id=community.id,
        remote_actor_id=f"{ORIGIN}/actors/services/other",
        remote_inbox_url=f"{ORIGIN}/actors/services/other/inbox",
        follow_activity_id=f"{ORIGIN}/follow/other",
    )
    result = await authorize_inbound_source(
        event=_event(), local_community_id=community.id,
        database=db, policy=policy, fetch_actor=_fetch(),
    )
    assert result is None
    db.close()


@pytest.mark.asyncio
async def test_another_group_without_service_membership_denies_person(tmp_path):
    """Service access to Group A does not grant access to unrelated Group B."""
    db, _, _, policy = _db(tmp_path)
    other = db.local_communities.create_local_community(
        discord_guild_id=10, discord_forum_channel_id=12, slug="other", display_name="Other",
        summary=None, created_by_discord_user_id="12",
        actor_url=HOST.replace("forum", "other"),
        inbox_url=HOST.replace("forum", "other") + "/inbox",
        outbox_url=HOST.replace("forum", "other") + "/outbox",
        followers_url=HOST.replace("forum", "other") + "/followers",
        public_key_pem="pub", private_key_pem="priv",
    )
    result = await authorize_inbound_source(
        event=_event(), local_community_id=other.id,
        database=db, policy=policy, fetch_actor=_fetch(),
    )
    assert result is None
    db.close()


@pytest.mark.asyncio
async def test_blocked_origin_denies_before_actor_fetch(tmp_path):
    """Federation policy protects the actor-document network boundary."""
    db, community, _, _ = _db(tmp_path)
    policy = BridgePolicyService(settings=SimpleNamespace(
        federation_allowlist=[], federation_blocklist=["remote.example"],
        discord_guild_allowlist=[], discord_guild_blocklist=[],
        bridge_super_admin_user_ids=[],
    ), repository=db.bridge_policy_entries)
    async def no_fetch(*_args):
        raise AssertionError("blocked origin must not be contacted")
    result = await authorize_inbound_source(
        event=_event(), local_community_id=community.id,
        database=db, policy=policy, fetch_actor=no_fetch,
    )
    assert result is None
    db.close()


@pytest.mark.asyncio
async def test_signed_person_reply_through_service_uses_verified_parent(tmp_path):
    """A signed Bridge Person reply reaches the host only through its local Group root."""
    db, community, _, policy = _db(tmp_path)
    created = SimpleNamespace(id=801)
    starter = SimpleNamespace(id=802)
    forum = SimpleNamespace(create_thread=AsyncMock(return_value=(created, starter)))
    thread = SimpleNamespace(id=801, guild_id=10, send=AsyncMock(return_value=SimpleNamespace(id=803)))
    bot = SimpleNamespace(
        wait_until_bridge_ready=AsyncMock(),
        fetch_forum_channel=AsyncMock(return_value=forum),
        get_thread_by_id=AsyncMock(return_value=thread),
    )
    handler = LocalCommunityInboundCreateHandler(
        database=db, surface_fanout=AsyncMock(), federation_fanout=AsyncMock(),
        discord_client_provider=lambda: bot, bridge_policy_service=policy,
        actor_fetcher=_fetch(),
    )
    assert (await handler.handle_post(_event(), runtime=None)).status == "processed"
    comment_id = PERSON + "/objects/comments/reply-1"
    reply = ActivityPubEvent.model_validate({
        "event_type": "comment.created",
        "delivery_id": PERSON + "/activities/create/comments/reply-1",
        "occurred_at": "2026-11-12T01:05:00Z",
        "community_actor_id": HOST, "verified_destination_group_actor_id": HOST,
        "verified_sender_actor_id": PERSON, "actor_id": PERSON,
        "source_activity_json": {"type": "Create", "actor": PERSON,
            "object": {"type": "Note", "id": comment_id, "attributedTo": PERSON}},
        "object": {"ap_id": comment_id, "kind": "comment", "lemmy_id": 0,
            "post_ap_id": OBJECT, "post_lemmy_id": 0, "parent_ap_id": OBJECT,
            "published_at": "2026-11-12T01:05:00Z", "author_name": "alice",
            "body_markdown": "reply", "url": comment_id,
            "attributed_to_actor_id": PERSON},
    })
    result = await handler.handle_comment(reply, runtime=None)
    assert result.status == "processed"
    message = db.local_community_content.get_local_community_message_by_ap_object_id(comment_id)
    assert message is not None and message.local_community_id == community.id
    assert message.parent_ap_object_id == OBJECT
    thread.send.assert_awaited_once()
    # A false sender does not create the same reply in another Group or resend.
    invalid = await handler.handle_comment(reply.model_copy(update={
        "verified_sender_actor_id": SERVICE,
    }), runtime=None)
    assert invalid.outcome is InboundActivityOutcome.IGNORED_ACTOR_NOT_SUBSCRIBER
    thread.send.assert_awaited_once()
    db.close()
