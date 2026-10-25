"""Inbound ActivityPub replay must not fanout or relay with forged new fields."""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace, ModuleType
from dataclasses import dataclass

import pytest
from support.fake_platform_adapter import FakeDiscordPort
from src.platform_types import CreatedThread, CreatedMessage, ThreadRef, MessageRef
from src.models import PlatformDeliveryIntent
from sqlalchemy import select

from test_local_receipt_persistence import world, thread_arguments, message_arguments


class ExternalEffects:
    """Stateful outer Discord/federation boundaries recording physical effects."""

    def __init__(self):
        """Observe actual attempted physical/federated side effects."""
        self.calls = []
        self.policy_service = SimpleNamespace(is_discord_guild_allowed=lambda guild: guild == 10)

    async def fanout_thread_to_local_subscribers(self, **kwargs):
        """Record any attempted sibling Discord thread creation."""
        self.calls.append(('discord-thread',kwargs))

    async def fanout_message_to_local_subscribers(self, **kwargs):
        """Record any attempted sibling Discord message creation."""
        self.calls.append(('discord-message',kwargs))

    async def relay_create(self, **kwargs):
        """Record any attempted ActivityPub Announce side effect."""
        self.calls.append(('relay',kwargs))


def _handler(db, effects, monkeypatch):
    """Instantiate real inbound handler with only missing SDK/network edges faked."""
    handler_cls = importlib.import_module(
        'src.local_communities.inbound_create').LocalCommunityInboundCreateHandler
    class Transport(FakeDiscordPort):
        """Accept only exact typed subscriber destinations, never a duplicate host."""
        async def create_forum_thread(self, forum, content, *, title, presentation, attempt):
            """Return one subscriber thread/starter receipt."""
            assert forum.external_channel_id == '500'
            effects.calls.append(('discord-thread', forum))
            return CreatedThread(ThreadRef(forum, '501'), MessageRef('discord-primary', '501', '502'), 'bot')
        async def send_message(self, destination, content, *, reply_to, presentation, attempt):
            """Acknowledge the exact reply in the existing subscriber thread."""
            assert destination.external_thread_id == '501'
            effects.calls.append(('discord-message', destination))
            return CreatedMessage(MessageRef('discord-primary', '501', '503'), 'bot')
    handler = handler_cls(database=db, surface_fanout=effects,
                       federation_fanout=effects, discord_client_provider=lambda: None)
    handler.bind_discord_adapter(Transport())
    return handler


def _post(*, activity_id='https://remote.test/create/post',
          actor_id='https://remote.test/u/bob',
          obj_id='https://remote.test/post',
          body='Hello', title='title', source=None):
    """Build one normalized Create with claimed author, AP identity and source."""
    source = source if source is not None else {'id':activity_id, 'type':'Create',
                                                'actor':actor_id, 'object':{'id':obj_id,'content':body}}
    return SimpleNamespace(
        delivery_id=activity_id, source_activity_id=activity_id,
        source_announce_id=None, source_activity_json=source,
        community_actor_id='https://bridge.test/actors/communities/hackers', actor_id=actor_id,
        object=SimpleNamespace(ap_id=obj_id, kind='post', title=title,
                               body_markdown=body, post_ap_id=None,
                               parent_ap_id=None, author_name='bob'),
    )


def _accepted(db, community_id, actor_id='https://remote.test/u/bob'):
    """Authorize one actor using the actual persisted subscriber repository."""
    db.remote_subscribers.create_remote_subscriber(
        local_community_id=community_id, remote_actor_id=actor_id,
        remote_inbox_url='https://remote.test/inbox',
        follow_activity_id='https://remote.test/follow', status='accepted')


@pytest.mark.asyncio
async def test_forged_duplicate_post_activity_and_author_never_relay(tmp_path, monkeypatch):
    """After one post is stored, same object/different source has no side effects."""
    db, community_id = world(tmp_path)
    db.local_community_content.create_local_community_thread(**thread_arguments(community_id))
    _accepted(db, community_id)
    effects = ExternalEffects()
    handler = _handler(db, effects, monkeypatch)
    conflict = await handler.handle_post(_post(activity_id='https://forged.test/create'), None)
    assert conflict.detail in ('mapping_conflict', 'unverified_duplicate')
    assert effects.calls == []
    forged = await handler.handle_post(_post(actor_id='https://forged.test/u/mallory'), None)
    assert forged.status == 'skipped'
    assert effects.calls == []
    db.close()


@pytest.mark.asyncio
async def test_legacy_post_without_source_provenance_skips_replay(tmp_path, monkeypatch):
    """Historical stored canonical post cannot gain fresh author proof on replay."""
    db, community_id = world(tmp_path)
    db.local_community_content.create_local_community_thread(**thread_arguments(community_id))
    _accepted(db, community_id)
    effects = ExternalEffects()
    handler = _handler(db, effects, monkeypatch)
    result = await handler.handle_post(_post(), None)
    assert result.detail == 'unverified_duplicate'
    assert effects.calls == []
    db.close()


@pytest.mark.asyncio
async def test_revoked_subscriber_cannot_replay_existing_post(tmp_path, monkeypatch):
    """Current Follow status is authoritative even when canonical IDs match."""
    db, community_id = world(tmp_path)
    db.local_community_content.create_local_community_thread(**thread_arguments(community_id))
    effects = ExternalEffects()
    handler = _handler(db, effects, monkeypatch)
    result = await handler.handle_post(_post(), None)
    assert result.detail == 'remote actor is not an accepted remote subscriber'
    assert effects.calls == []
    db.close()


@pytest.mark.asyncio
async def test_existing_comment_changed_actor_parent_cannot_relay(tmp_path, monkeypatch):
    """One persisted comment cannot be replayed under a different actor/parent."""
    db, community_id = world(tmp_path)
    thread = db.local_community_content.create_local_community_thread(**thread_arguments(community_id))
    db.local_community_content.create_local_community_message(**message_arguments(thread.id))
    _accepted(db, community_id)
    effects = ExternalEffects()
    handler = _handler(db, effects, monkeypatch)
    post = _post()
    post.delivery_id = 'https://remote.test/create/comment'
    post.source_activity_id = post.delivery_id
    post.object.ap_id = 'https://remote.test/comment'
    post.object.kind = 'comment'
    post.object.post_ap_id = 'https://remote.test/post'
    post.object.parent_ap_id = 'https://forged.test/parent'
    result = await handler.handle_comment(post, None)
    assert result.detail in ('mapping_conflict','unverified_duplicate')
    assert effects.calls == []
    db.close()

@pytest.mark.asyncio
async def test_matching_post_with_retained_source_can_resume_fanout(tmp_path, monkeypatch):
    """A proven canonical replay restores missing sibling/relay work without host POST."""
    db, community_id = world(tmp_path)
    db.local_community_content.create_local_community_thread(**thread_arguments(community_id))
    subscriber = db.local_subscribers.create_local_subscriber(local_community_id=community_id,
        discord_guild_id=10, discord_channel_id=500, initiated_by_discord_user_id=None)
    _accepted(db, community_id)
    event = _post()
    db.local_community_relay.get_or_create_local_community_relay_source_activity(
        local_community_id=community_id, operation='create', object_kind='post',
        source_object_ap_id=event.object.ap_id, source_activity_id=event.source_activity_id,
        source_announce_id=None, origin_remote_actor_id=event.actor_id,
        source_activity_json=event.source_activity_json)
    effects = ExternalEffects()
    result = await _handler(db, effects, monkeypatch).handle_post(event, None)
    assert result.detail == 'post already mapped'
    assert [kind for kind, _ in effects.calls] == ['discord-thread', 'relay']
    with db.session() as session:
        intent = session.scalar(select(PlatformDeliveryIntent))
        assert intent is not None and intent.state == 'delivered'
    db.close()


@pytest.mark.asyncio
async def test_matching_comment_repairs_missing_generic_mapping_without_host_send(tmp_path, monkeypatch):
    """A valid mapped comment can restore its generic AP mapping from host receipt."""
    db, community_id = world(tmp_path)
    thread = db.local_community_content.create_local_community_thread(**thread_arguments(community_id))
    db.local_community_content.create_local_community_message(**message_arguments(thread.id))
    subscriber = db.local_subscribers.create_local_subscriber(local_community_id=community_id,
        discord_guild_id=10, discord_channel_id=500, initiated_by_discord_user_id=None)
    _accepted(db, community_id)
    db.local_community_surfaces.create_local_community_thread_surface(
        local_community_thread_id=thread.id, discord_forum_channel_id=500,
        discord_thread_id=501, discord_starter_message_id=502,
        local_subscriber_id=subscriber.id, role='local_subscriber')
    event = _post(activity_id='https://remote.test/create/comment',
                  obj_id='https://remote.test/comment')
    event.object.kind = 'comment'
    event.object.post_ap_id = 'https://remote.test/post'
    event.object.parent_ap_id = 'https://remote.test/post'
    admission = db.personal_feed_conversations.retain_community_create(
        activity_id=event.source_activity_id, object_id=event.object.ap_id,
        root_object_id=event.object.post_ap_id, parent_object_id=event.object.parent_ap_id,
        actor_id=event.actor_id, actor_name=event.object.author_name,
        content_markdown=event.object.body_markdown, published_at=None,
        source_activity=event.source_activity_json)
    assert admission.status == 'created'
    effects = ExternalEffects()
    result = await _handler(db, effects, monkeypatch).handle_comment(event, None)
    assert result.detail == 'comment already mapped'
    mapping = db.message_mappings.get_message_mapping_by_object_id(event.object.ap_id)
    assert mapping is not None
    assert mapping.discord_message_id == 700
    assert [kind for kind, _ in effects.calls] == ['discord-message', 'relay']
    with db.session() as session:
        intent = session.scalar(select(PlatformDeliveryIntent))
        assert intent is not None and intent.state == 'delivered'
    db.close()
