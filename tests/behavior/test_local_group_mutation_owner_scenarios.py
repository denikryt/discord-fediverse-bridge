"""CP22: signed local Group mutations require immutable tenant and Create ownership.

These scenarios use real SQLite canonical rows and real mutation admission. Only
Discord and federated delivery are replaced with observability fakes.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.activitypub_models import ActivityPubEvent
from src.bridge_policy import BridgePolicyService
from src.db import Database
from src.local_communities.mutations import LocalCommunityMutationHandler

GROUP_A = "https://bridge.example/actors/communities/a"
GROUP_B = "https://bridge.example/actors/communities/b"
AUTHOR = "https://remote.example/actors/users/alice"
OTHER = "https://remote.example/actors/users/bob"
POST = AUTHOR + "/objects/posts/1"
REPLY = AUTHOR + "/objects/comments/1"


def _setup(tmp_path):
    """Create two tenant-owned host surfaces with the same external object IDs."""
    db = Database(f"sqlite:///{tmp_path / 'mutations.sqlite'}")
    db.create_all()
    groups = []
    for number, group_actor in enumerate((GROUP_A, GROUP_B), start=1):
        community = db.local_communities.create_local_community(
            discord_guild_id=number, discord_forum_channel_id=200 + number,
            slug=f"community-{number}", display_name=f"Community {number}", summary=None,
            created_by_discord_user_id="3", actor_url=group_actor,
            inbox_url=group_actor + "/inbox", outbox_url=group_actor + "/outbox",
            followers_url=group_actor + "/followers", public_key_pem="pub", private_key_pem="priv",
        )
        db.remote_subscribers.create_remote_subscriber(
            local_community_id=community.id, remote_actor_id=AUTHOR,
            remote_inbox_url=AUTHOR + "/inbox", follow_activity_id=f"https://remote.example/follow/{number}",
        )
        root = db.local_community_content.reserve_inbound_thread(
            local_community_id=community.id,
            ap_activity_id=AUTHOR + "/activities/create/1", ap_object_id=POST,
            source_owner_actor_id=AUTHOR, origin_subscriber_actor_id=AUTHOR,
            inbound_create_snapshot={"title": "Original", "content": "post body"},
        )
        db.local_community_content.finish_inbound_host_attempt(
            canonical_id=root.canonical.id, attempt_id=root.attempt_id, kind="post",
            state="delivered", discord_thread_id=500 + number,
            discord_message_id=600 + number,
        )
        reply = db.local_community_content.reserve_inbound_message(
            local_community_id=community.id, local_community_thread_id=root.canonical.id,
            ap_activity_id=AUTHOR + "/activities/create-comment/1", ap_object_id=REPLY,
            parent_ap_object_id=POST, source_owner_actor_id=AUTHOR,
            origin_subscriber_actor_id=AUTHOR,
            inbound_create_snapshot={"content": "reply body"},
        )
        db.local_community_content.finish_inbound_host_attempt(
            canonical_id=reply.canonical.id, attempt_id=reply.attempt_id, kind="comment",
            state="delivered", discord_message_id=700 + number,
            parent_discord_message_id=600 + number,
        )
        groups.append((community, root.canonical, reply.canonical))
    policy = BridgePolicyService(settings=SimpleNamespace(
        federation_allowlist=[], federation_blocklist=[], discord_guild_allowlist=[],
        discord_guild_blocklist=[], bridge_super_admin_user_ids=[],
    ), repository=db.bridge_policy_entries)
    surfaces = SimpleNamespace(
        fanout_thread_edit=AsyncMock(), fanout_thread_delete=AsyncMock(),
        fanout_message_edit=AsyncMock(), fanout_message_delete=AsyncMock(),
    )
    relay = SimpleNamespace(relay_update_or_delete=AsyncMock())
    handler = LocalCommunityMutationHandler(
        database=db, fedify_gateway=AsyncMock(), bridge_policy_service=policy,
        surface_fanout=surfaces, federation_fanout=relay,
        is_community_id_disabled=lambda _id: False,
    )
    return db, groups, handler, surfaces, relay


def _event(group=GROUP_A, *, operation="Update", kind="post", author=AUTHOR,
           verified=True, wrapped=False, root=POST, parent=POST,
           object_owner=None, source_actor=None, source_type=None, signed_parent=None, id_only_delete=False):
    """One independently verified owner action with explicit root/parent context."""
    ap_id = POST if kind == "post" else REPLY
    obj = {"id": ap_id}
    if operation == "Update":
        obj.update(type="Note", attributedTo=object_owner or author)
        if signed_parent is not None:
            obj["inReplyTo"] = signed_parent
    else:
        obj.update(type="Tombstone")
    data = {
        "event_type": ("post" if kind == "post" else "comment") + (
            ".updated" if operation == "Update" else ".deleted"),
        "delivery_id": AUTHOR + f"/activities/{operation.lower()}/{kind}/{group.rsplit('/', 1)[-1]}",
        "occurred_at": "2026-11-12T01:00:00Z", "community_actor_id": group,
        "verified_destination_group_actor_id": group,
        "actor_id": author, "verified_sender_actor_id": author if verified else None,
        "source_announce_id": AUTHOR + "/activities/announce/1" if wrapped else None,
        "source_activity_json": {
            "type": source_type or operation, "actor": source_actor or author,
            "object": ap_id if id_only_delete else obj,
        },
        "object": {
            "ap_id": ap_id, "kind": kind, "lemmy_id": 0,
            "published_at": "2026-11-12T01:00:00Z", "author_name": "alice",
            "attributed_to_actor_id": object_owner or author,
            "body_markdown": "updated", "url": ap_id, "post_ap_id": root if kind == "comment" else None,
            "parent_ap_id": parent if kind == "comment" else None,
            "post_lemmy_id": 0 if kind == "comment" else None,
        },
    }
    return ActivityPubEvent.model_validate(data)


async def _act(handler, event):
    """Exercise the real mutation handler, selecting root/reply and verb."""
    suffix = "update" if event.event_type.endswith("updated") else "delete"
    kind = event.object.kind
    return await getattr(handler, f"handle_inbound_{kind}_{suffix}")(event, None)


def _no_effects(surfaces, relay):
    """No Discord edit/delete or federation relay may run on a denied mutation."""
    for name in ("fanout_thread_edit", "fanout_thread_delete", "fanout_message_edit", "fanout_message_delete"):
        getattr(surfaces, name).assert_not_awaited()
    relay.relay_update_or_delete.assert_not_awaited()


@pytest.mark.parametrize("kind", ["post", "comment"])
@pytest.mark.parametrize("operation", ["Update", "Delete"])
@pytest.mark.asyncio
async def test_signed_owner_mutates_only_the_correct_tenant(tmp_path, kind, operation):
    """Owner action in Group B edits one B surface despite identical Group A AP IDs."""
    db, groups, handler, surfaces, relay = _setup(tmp_path)
    result = await _act(handler, _event(GROUP_B, operation=operation, kind=kind))
    assert result.status == "processed"
    name = f"fanout_{'thread' if kind == 'post' else 'message'}_{'edit' if operation == 'Update' else 'delete'}"
    expected = groups[1][1] if kind == "post" else groups[1][2]
    assert getattr(surfaces, name).await_args.kwargs[f"{'thread' if kind == 'post' else 'message'}_row"].id == expected.id
    relay.relay_update_or_delete.assert_awaited_once()
    db.close()


@pytest.mark.parametrize("alteration", [
    {"author": OTHER},
    {"source_actor": OTHER},
    {"verified": False},
    {"wrapped": True},
    {"source_type": "Create"},
    {"object_owner": OTHER},
])
@pytest.mark.asyncio
async def test_unsigned_or_foreign_person_does_not_mutate_any_surface(tmp_path, alteration):
    """Neither same-host impersonation nor nested Group Announce grants owner rights."""
    db, groups, handler, surfaces, relay = _setup(tmp_path)
    result = await _act(handler, _event(GROUP_A, **alteration))
    assert result.status == "skipped"
    _no_effects(surfaces, relay)
    db.close()


@pytest.mark.parametrize("operation", ["Update", "Delete"])
@pytest.mark.asyncio
async def test_revoked_subscriber_cannot_mutate_previous_post(tmp_path, operation):
    """A valid original owner loses publishing rights after a signed Undo."""
    db, groups, handler, surfaces, relay = _setup(tmp_path)
    db.remote_subscribers.delete_remote_subscriber(
        local_community_id=groups[0][0].id, remote_actor_id=AUTHOR,
    )
    result = await _act(handler, _event(GROUP_A, operation=operation))
    assert result.status == "skipped"
    _no_effects(surfaces, relay)
    db.close()


@pytest.mark.asyncio
async def test_cross_group_reply_parent_is_rejected(tmp_path):
    """A signed comment cannot switch the original parent or root by fresh metadata."""
    db, groups, handler, surfaces, relay = _setup(tmp_path)
    result = await _act(handler, _event(GROUP_A, kind="comment", parent=OTHER + "/post/1"))
    assert result.status == "skipped"
    _no_effects(surfaces, relay)
    db.close()


@pytest.mark.asyncio
async def test_legacy_unknown_owner_cannot_be_claimed_by_fresh_update(tmp_path):
    """A legacy NULL owner row cannot gain an owner from a new signed envelope."""
    from sqlalchemy import update
    from src.models import LocalCommunityThread
    db, groups, handler, surfaces, relay = _setup(tmp_path)
    with db.session() as session:
        session.execute(update(LocalCommunityThread).where(
            LocalCommunityThread.id == groups[0][1].id
        ).values(source_owner_actor_id=None))
    result = await _act(handler, _event(GROUP_A))
    assert result.status == "skipped"
    _no_effects(surfaces, relay)
    db.close()


@pytest.mark.parametrize("kind", ["post", "comment"])
@pytest.mark.asyncio
async def test_signed_bridge_person_mutation_uses_original_service_not_hostname(tmp_path, kind):
    """A registered Person may edit via exactly its previously accepted Service."""
    from sqlalchemy import update
    from src.models import LocalCommunityThread, LocalCommunityMessage

    db, groups, handler, surfaces, relay = _setup(tmp_path)
    service = "https://remote.example/actors/services/bridge"
    community = groups[0][0]
    # Model the CP20 initial Create: the immutable owner was Person, while the
    # accepted relationship and original relay exclusion key were Service.
    db.remote_subscribers.delete_remote_subscriber(
        local_community_id=community.id, remote_actor_id=AUTHOR,
    )
    db.remote_subscribers.create_remote_subscriber(
        local_community_id=community.id, remote_actor_id=service,
        remote_inbox_url=service + "/inbox",
        follow_activity_id="https://remote.example/follow/bridge",
    )
    with db.session() as session:
        session.execute(update(LocalCommunityThread).where(
            LocalCommunityThread.id == groups[0][1].id,
        ).values(origin_subscriber_actor_id=service))
        session.execute(update(LocalCommunityMessage).where(
            LocalCommunityMessage.id == groups[0][2].id,
        ).values(origin_subscriber_actor_id=service))

    def doc(actor_id, kind, *, bridge=False):
        result = {
            "type": kind, "id": actor_id, "inbox": actor_id + "/inbox",
            "outbox": actor_id + "/outbox",
            "endpoints": {"sharedInbox": "https://remote.example/inbox"},
            "publicKey": {"id": actor_id + "#key", "owner": actor_id,
                          "publicKeyPem": "-----BEGIN PUBLIC KEY-----..."},
        }
        if bridge:
            result["attachment"] = [{"type": "PropertyValue", "name": "Bridge software",
                                     "value": "discord-fediverse-bridge"}]
        return result

    async def fetch(actor_id, _accept):
        return {AUTHOR: doc(AUTHOR, "Person"), service: doc(service, "Service", bridge=True)}[actor_id]

    handler.actor_fetcher = fetch
    event = _event(GROUP_A, kind=kind)
    result = await _act(handler, event)
    assert result.status == "processed"
    relay.relay_update_or_delete.assert_awaited_once()

    # A later signed Undo eliminates permission without rewriting the already
    # committed canonical owner or granting a same-origin stranger access.
    db.remote_subscribers.delete_remote_subscriber(
        local_community_id=community.id, remote_actor_id=service,
    )
    surfaces.fanout_message_edit.reset_mock()
    surfaces.fanout_thread_edit.reset_mock()
    relay.relay_update_or_delete.reset_mock()
    denied = await _act(handler, event)
    assert denied.status == "skipped"
    _no_effects(surfaces, relay)
    db.close()


@pytest.mark.asyncio
async def test_signed_reply_root_switch_is_rejected_even_when_parent_matches(tmp_path):
    """The Group root remains the immutable original Create root for updates."""
    db, groups, handler, surfaces, relay = _setup(tmp_path)
    result = await _act(handler, _event(GROUP_A, kind="comment", root=OTHER + "/post/2"))
    assert result.status == "skipped"
    _no_effects(surfaces, relay)
    db.close()


@pytest.mark.asyncio
async def test_id_only_signed_tombstone_cannot_claim_legacy_message_without_owner(tmp_path):
    """Even verified Delete(Tombstone) must not infer missing legacy provenance."""
    from sqlalchemy import update
    from src.models import LocalCommunityMessage
    db, groups, handler, surfaces, relay = _setup(tmp_path)
    with db.session() as session:
        session.execute(update(LocalCommunityMessage).where(
            LocalCommunityMessage.id == groups[0][2].id
        ).values(source_owner_actor_id=None))
    result = await _act(handler, _event(GROUP_A, kind="comment", operation="Delete"))
    assert result.status == "skipped"
    _no_effects(surfaces, relay)
    db.close()


@pytest.mark.asyncio
async def test_signed_reply_update_cannot_hide_a_changed_parent_in_normalization(tmp_path):
    """A verified Update with different inReplyTo cannot borrow the canonical parent."""
    db, _groups, handler, surfaces, relay = _setup(tmp_path)
    # The normalized parent remains unchanged but the source signed action
    # names a different parent; the signed envelope must take precedence.
    result = await _act(handler, _event(
        GROUP_A, kind="comment", signed_parent=OTHER + "/objects/posts/foreign",
    ))
    assert result.status == "skipped"
    _no_effects(surfaces, relay)
    db.close()


@pytest.mark.asyncio
async def test_id_only_signed_delete_preserves_original_owner_and_tenant(tmp_path):
    """A direct owner-signed Delete IRI is permitted only for its canonical Group."""
    db, groups, handler, surfaces, relay = _setup(tmp_path)
    result = await _act(handler, _event(
        GROUP_B, operation="Delete", kind="comment", id_only_delete=True,
    ))
    assert result.status == "processed"
    kwargs = surfaces.fanout_message_delete.await_args.kwargs
    assert kwargs["message_row"].id == groups[1][2].id
    relay.relay_update_or_delete.assert_awaited_once()
    db.close()


@pytest.mark.asyncio
async def test_different_person_on_same_accepted_service_cannot_edit_original(tmp_path):
    """Service authorization never grants one registered Person another's object."""
    from sqlalchemy import update
    from src.models import LocalCommunityMessage, LocalCommunityThread

    db, groups, handler, surfaces, relay = _setup(tmp_path)
    service = "https://remote.example/actors/services/bridge"
    tenant = groups[0][0].id
    db.remote_subscribers.delete_remote_subscriber(local_community_id=tenant, remote_actor_id=AUTHOR)
    db.remote_subscribers.create_remote_subscriber(
        local_community_id=tenant, remote_actor_id=service,
        remote_inbox_url=service + "/inbox", follow_activity_id="https://remote.example/follows/2",
    )
    with db.session() as session:
        session.execute(update(LocalCommunityThread).where(
            LocalCommunityThread.id == groups[0][1].id,
        ).values(origin_subscriber_actor_id=service))
        session.execute(update(LocalCommunityMessage).where(
            LocalCommunityMessage.id == groups[0][2].id,
        ).values(origin_subscriber_actor_id=service))

    def actor_doc(actor_id, actor_type):
        """Serve one verified Bridge actor with an owned key and shared inbox."""
        doc = {
            "id": actor_id, "type": actor_type,
            "inbox": actor_id + "/inbox", "outbox": actor_id + "/outbox",
            "endpoints": {"sharedInbox": "https://remote.example/inbox"},
            "publicKey": {"id": actor_id + "#key", "owner": actor_id, "publicKeyPem": "pub"},
        }
        if actor_type == "Service":
            doc["attachment"] = [{
                "type": "PropertyValue", "name": "Bridge software", "value": "discord-fediverse-bridge",
            }]
        return doc

    async def fetch(actor_id, _accept):
        """Simulate safe public actor retrieval at the external network boundary."""
        return actor_doc(actor_id, "Service" if actor_id == service else "Person")

    handler.actor_fetcher = fetch
    result = await _act(handler, _event(GROUP_A, kind="comment", author=OTHER))
    assert result.status == "skipped"
    _no_effects(surfaces, relay)
    db.close()


@pytest.mark.asyncio
async def test_missing_verified_recipient_cannot_use_an_existing_host_surface(tmp_path):
    """A known AP object is not authorization to choose a tenant's host surface."""
    db, _groups, handler, surfaces, relay = _setup(tmp_path)
    forged = _event(GROUP_A).model_copy(update={"verified_destination_group_actor_id": None})
    result = await _act(handler, forged)
    assert result.status == "skipped"
    _no_effects(surfaces, relay)
    db.close()


@pytest.mark.asyncio
async def test_real_discord_surface_fanout_updates_only_the_verified_group(tmp_path):
    """A signed Group B mutation edits its saved Discord surface, not Group A's."""
    from src.local_communities.discord_surface_fanout import LocalCommunitySurfaceFanout

    db, groups, handler, _surfaces, relay = _setup(tmp_path)
    messages = {
        601: SimpleNamespace(content="Group A original", edit=AsyncMock()),
        602: SimpleNamespace(content="Group B original", edit=AsyncMock()),
    }

    async def fetch_thread(thread_id):
        """Resolve a real persisted thread identity against one Discord boundary fake."""
        # Discord channel 501 belongs exclusively to Group A and 502 to B.
        expected_message_id = {501: 601, 502: 602}[thread_id]
        return SimpleNamespace(fetch_message=AsyncMock(
            side_effect=lambda msg_id: messages[msg_id] if msg_id == expected_message_id
            else (_ for _ in ()).throw(KeyError(msg_id)),
        ))

    bot = SimpleNamespace(get_thread_by_id=AsyncMock(side_effect=fetch_thread))
    handler.surface_fanout = LocalCommunitySurfaceFanout(
        database=db, policy_service=handler.bridge_policy_service,
        discord_client_provider=lambda: bot,
    )
    result = await _act(handler, _event(GROUP_B))
    assert result.status == "processed"
    messages[602].edit.assert_awaited_once()
    assert "updated" in messages[602].edit.await_args.kwargs["content"]
    messages[601].edit.assert_not_awaited()
    relay.relay_update_or_delete.assert_awaited_once()

    # Changing the tenant does not prove ownership of the other Group. This
    # revoked subscriber still cannot touch the previously delivered surface.
    db.remote_subscribers.delete_remote_subscriber(
        local_community_id=groups[1][0].id, remote_actor_id=AUTHOR,
    )
    relay.relay_update_or_delete.reset_mock()
    denied = await _act(handler, _event(GROUP_B, operation="Delete"))
    assert denied.status == "skipped"
    messages[601].edit.assert_not_awaited()
    messages[602].edit.assert_awaited_once()
    relay.relay_update_or_delete.assert_not_awaited()
    db.close()
