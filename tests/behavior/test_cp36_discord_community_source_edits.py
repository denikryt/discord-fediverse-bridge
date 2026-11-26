"""Native Discord Community starter edits through the actual raw event path.

Real SQLite source/delivery/owner rows; fake only the Discord SDK boundary and
outgoing durable mutation service. Source Message/Delete semantics are untouched.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

# Source handlers only use discord.HTTPException at this outer boundary. The
# real SDK takes precedence when installed; no package is added to production.
try:
    import discord
except ImportError:
    discord = ModuleType('discord')
    discord.HTTPException = type('HTTPException', (Exception,), {})
    sys.modules['discord'] = discord

import pytest
from sqlalchemy import select

from src.db import Database
from src.discord_event_router import DiscordEventRouter
from src.discord_message_mutations import DiscordMessageMutationHandler
from src.community_sync.discord_fanout import DiscordFanout
from src.community_sync.runtime import CommunityRuntime
from src.models import MessageMapping, PublishedActivityObject
from src.runtime_ports import DiscordMutationContext

GROUP = 'https://nodebb.example/category/2'
ACTOR = 'https://bridge.example/users/alice'
OBJECT = 'https://bridge.example/objects/posts/one'
ACTIVITY = 'https://bridge.example/activities/create/one'
EDITED = '2026-11-19T13:20:00+00:00'


class _Policy:
    """Accept exactly the configured test Guild and the remote Group."""

    guild_allowed = True
    group_allowed = True

    def is_discord_guild_allowed(self, guild_id):
        return self.guild_allowed and guild_id == 7

    def federation_decision(self, _actor):
        return SimpleNamespace(allowed=self.group_allowed)


class _MutationService:
    """Record the durable outbound admission arguments at the gateway boundary."""

    def __init__(self):
        self.calls = []
        self.outcome = 'accepted'

    async def submit_update(self, **kwargs):
        self.calls.append(kwargs)
        return self.outcome


class _Wake:
    """Count scheduler notifications after a successful canonical reservation."""

    def __init__(self):
        self.calls = 0

    def wake(self):
        self.calls += 1


class _Message:
    """Expose only Discord's current text and mutable mirror identity."""

    def __init__(self, content, *, edited_at=None):
        self.content = content
        self.edited_at = edited_at
        self.edits = []
        self.suppressed = True

    async def edit(self, *, content, suppress=False):
        self.content = content
        self.edits.append(content)
        self.suppressed = suppress


class _Thread:
    """Fetch a retained starter by the exact Discord message identifier."""

    def __init__(self, message_id, message):
        self.message_id = message_id
        self.message = message

    async def fetch_message(self, message_id):
        assert message_id == self.message_id
        return self.message


class _DiscordEdge:
    """Stub only Discord GET; raw events and SQLite are real handlers."""

    def __init__(self):
        self.source = _Message('source text', edited_at=datetime.fromisoformat(EDITED))
        self.mirror = _Message('`Alice`\n\noriginal text')
        self.channels = {111: _Thread(111, self.source), 222: _Thread(222, self.mirror)}
        self.tracked = []
        self.database = None

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        return self.channels[channel_id]

    async def get_thread_by_id(self, thread_id):
        return self.channels[thread_id]

    def track_message_edit(self, message_id):
        self.tracked.append(message_id)


class _LocalRuntime:
    """Reject accidental fallback to local-community message editing."""

    async def handle_discord_message_edit(self, **_kwargs):
        raise AssertionError('remote Community starter routed to local Community')


def _setup(tmp_path: Path):
    """Seed the original Create, exact source starter and one bot mirror."""
    db = Database(f'sqlite:///{tmp_path / "starter.db"}')
    db.create_all()
    db.users.create_user(discord_user_id='4', activitypub_username='alice',
        actor_url=ACTOR, inbox_url=ACTOR+'/inbox', outbox_url=ACTOR+'/outbox',
        followers_url=ACTOR+'/followers', public_key_pem='test', private_key_pem='test')
    for channel in (10, 20):
        db.remote_subscriptions.create_subscription(discord_channel_id=channel,
            discord_guild_id=7, lemmy_community_actor_id=GROUP,
            lemmy_community_name='NodeBB', lemmy_community_id=None,
            status='accepted')
    group = db.discord_fanout_groups.create_thread_group(
        community_actor_id=GROUP, source_channel_id=10, source_thread_id=111,
        source_starter_message_id=111, ap_activity_id=ACTIVITY, ap_object_id=OBJECT)
    repo = db.discord_fanout_groups
    repo.add_thread_delivery(thread_group_id=group.id, discord_channel_id=10,
                             discord_thread_id=111, discord_starter_message_id=111, role='source')
    repo.add_thread_delivery(thread_group_id=group.id, discord_channel_id=20,
                             discord_thread_id=222, discord_starter_message_id=222, role='mirror')
    with db.session() as session:
        session.add(PublishedActivityObject(actor_username='alice', actor_url=ACTOR,
            publication_context='community', community_actor_url=GROUP,
            activity_id=ACTIVITY, object_id=OBJECT, kind='post',
            title='Original topic', body_markdown='original text',
            discord_message_id=111))
        session.add(MessageMapping(source_platform='discord', source_id='111',
            activity_id=ACTIVITY, object_id=OBJECT, actor_url=ACTOR,
            publication_context='community', community_actor_url=GROUP,
            discord_message_id=111))
    policy = _Policy()
    edge = _DiscordEdge()
    edge.database = db
    mutation = _MutationService()
    wake = _Wake()
    context = DiscordMutationContext(bridge_policy_service=policy,
        user_actor_delivery_service=mutation, user_actor_delivery_wake=wake,
        fedify_gateway=object())
    fanout = DiscordFanout(bot=edge, mutation_tracker=edge,
                           database=db, policy_service=policy)
    community = CommunityRuntime(database=db, content_publish_service=object(),
                                 discord_fanout=fanout)
    router = DiscordEventRouter(database=db, community_runtime=community,
                                local_community_runtime=_LocalRuntime(),
                                bridge_policy_service=policy)
    handler = DiscordMessageMutationHandler(database=db, event_router=router,
        discord_fetcher=edge, mutation_context_provider=lambda: context)
    return db, handler, policy, mutation, wake, edge


def _event(message_id=111, content='Hello again!', *, has_content=True,
           time=EDITED, guild=7):
    """Construct the same relevant shape as discord.py's raw update event."""
    data = {'edited_timestamp': time}
    if has_content:
        data['content'] = content
    return SimpleNamespace(message_id=message_id, channel_id=message_id,
                           guild_id=guild, data=data)


@pytest.mark.asyncio
async def test_native_starter_update_preserves_object_title_and_mirror_header(tmp_path: Path) -> None:
    """One source edit admits one Note revision and edits only its bot mirror."""
    db, handler, _policy, mutation, wake, edge = _setup(tmp_path)
    try:
        await handler.handle_raw_message_edit(_event())
        assert len(mutation.calls) == 1
        assert mutation.calls[0] == {'object_id': OBJECT, 'title': 'Original topic',
            'body_markdown': 'Hello again!', 'edited_timestamp': datetime.fromisoformat(EDITED)}
        assert wake.calls == 1
        assert edge.source.edits == []
        assert edge.mirror.content == '`Alice`\n\nHello again!'
        assert edge.mirror.suppressed
        assert edge.tracked == [222]
        # A new revision of the same raw event is suppressed by the existing
        # durable mutation service's idempotency, not by publishing a new Note.
        mutation.outcome = 'duplicate'
        await handler.handle_raw_message_edit(_event())
        assert len(mutation.calls) == 2 and wake.calls == 1
        assert len(edge.mirror.edits) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_empty_and_partial_source_edits_are_real_content_changes(tmp_path: Path) -> None:
    """An explicit empty string is not the absence of a raw content field."""
    db, handler, _policy, mutation, _wake, edge = _setup(tmp_path)
    try:
        await handler.handle_raw_message_edit(_event(content=''))
        assert mutation.calls[-1]['body_markdown'] == ''
        assert edge.mirror.content == '`Alice`'
        edge.source.content = 'Fetched actual text'
        await handler.handle_raw_message_edit(_event(has_content=False))
        assert mutation.calls[-1]['body_markdown'] == 'Fetched actual text'
        edge.source.edited_at = None
        await handler.handle_raw_message_edit(_event(has_content=False, time=None))
        assert len(mutation.calls) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_mirror_and_unmapped_starters_cannot_trigger_person_update(tmp_path: Path) -> None:
    """Only the native source starter's exact saved delivery is author-owned."""
    db, handler, _policy, mutation, wake, edge = _setup(tmp_path)
    try:
        await handler.handle_raw_message_edit(_event(message_id=222))
        await handler.handle_raw_message_edit(_event(message_id=999))
        assert mutation.calls == [] and wake.calls == 0
        assert edge.mirror.edits == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_disallowed_guild_policy_or_mismatched_actor_rejects_starter_edit(tmp_path: Path) -> None:
    """Expired rights and wrong saved actor mappings cannot send a mutation."""
    db, handler, policy, mutation, wake, edge = _setup(tmp_path)
    try:
        policy.guild_allowed = False
        await handler.handle_raw_message_edit(_event())
        policy.guild_allowed = True
        policy.group_allowed = False
        await handler.handle_raw_message_edit(_event())
        policy.group_allowed = True
        with db.session() as session:
            mapping = session.scalar(select(MessageMapping).where(
                MessageMapping.discord_message_id == 111))
            mapping.actor_url = 'https://attacker.example/users/x'
        await handler.handle_raw_message_edit(_event())
        assert mutation.calls == [] and wake.calls == 0
        assert edge.mirror.edits == []
    finally:
        db.close()

@pytest.mark.asyncio
async def test_new_starter_edit_admission_does_not_change_raw_delete_routing(tmp_path: Path) -> None:
    """The original MESSAGE_DELETE semantics are outside Plan 146's scope."""
    db, handler, _policy, mutation, wake, _edge = _setup(tmp_path)
    try:
        # In CP35 the source starter has no CommunityMessageGroupDelivery.
        # Only the newly added edit classifier may discover the root delivery.
        assert handler._classify_message(111)[0] is None
        assert handler._classify_message(111, include_source_starter=True)[0].role == 'source'
        await handler.handle_raw_message_delete(SimpleNamespace(message_id=111,
                                                                 channel_id=111, guild_id=7))
        assert mutation.calls == [] and wake.calls == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_partial_starter_uses_one_authoritative_body_timestamp_pair(tmp_path: Path) -> None:
    """A partial raw event may not combine a stale timestamp with a newer GET."""
    db, handler, _policy, mutation, _wake, edge = _setup(tmp_path)
    try:
        edge.source.content = 'Later authoritative content'
        edge.source.edited_at = datetime(2026, 11, 19, 13, 25, tzinfo=timezone.utc)
        await handler.handle_raw_message_edit(_event(has_content=False, time=EDITED))
        assert mutation.calls[-1]['body_markdown'] == 'Later authoritative content'
        assert mutation.calls[-1]['edited_timestamp'] == edge.source.edited_at
        await handler.handle_raw_message_edit(_event(content='Stale raw content', time=None))
        assert mutation.calls[-1]['body_markdown'] == 'Later authoritative content'
        assert mutation.calls[-1]['edited_timestamp'] == edge.source.edited_at
    finally:
        db.close()
