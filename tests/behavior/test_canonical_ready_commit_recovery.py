"""Rendered author mutations survive failed local commits without replaying render."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from src.db import Database
from src.fedify_gateway_client import CanonicalMutationResult
from src.models import PublishedActivityObject, UserActorContentMutation, UserActorOutboundSource
from src.platform_types import MessageRef
from src.user_actor_delivery import UserActorDeliveryService

ACTOR = 'https://bridge.test/actors/users/alice'
COMMUNITY = 'https://remote.test/c/group'
OBJECT = f'{ACTOR}/objects/posts/1'


class Renderer:
    """Return immutable render-only JSON, never perform inbox delivery."""
    def __init__(self):
        """Count renders separately from local SQLite attempts."""
        self.calls = 0

    async def update_content(self, request):
        """Respect the reservation's exact actor, object and activity identities."""
        self.calls += 1
        return CanonicalMutationResult(request.activity_id, request.ap_object_id,
            {'id': request.activity_id, 'type': 'Update', 'actor': ACTOR,
             'object': {'id': request.ap_object_id, 'content': request.body_markdown}})


def canonical(db, *, object_id=OBJECT, message_id=11):
    """Repair the same owning domain row without inventing rendered content."""
    group = db.discord_fanout_groups.create_thread_group(
        community_actor_id=COMMUNITY, source_channel_id=10,
        source_thread_id=message_id + 100, source_starter_message_id=message_id,
        ap_activity_id=f'{ACTOR}/activities/create/posts/{message_id}', ap_object_id=object_id)
    db.discord_fanout_groups.add_thread_delivery(thread_group_id=group.id,
        discord_channel_id=10, discord_thread_id=message_id + 100,
        discord_starter_message_id=message_id, role='source')
    return group


@pytest.fixture
def world(tmp_path):
    """Create a real typed source and durable author publication in SQLite."""
    db = Database(f'sqlite:///{tmp_path / "bridge.sqlite"}')
    db.migrate()
    db.users.create_user(discord_user_id='1', activitypub_username='alice', actor_url=ACTOR,
        inbox_url=f'{ACTOR}/inbox', outbox_url=f'{ACTOR}/outbox', followers_url=f'{ACTOR}/followers',
        public_key_pem='public', private_key_pem='private')
    db.remote_subscriptions.create_subscription(discord_channel_id=10, discord_guild_id=1,
        lemmy_community_actor_id=COMMUNITY, lemmy_community_name='group', lemmy_community_id=1,
        community_inbox_url=f'{COMMUNITY}/inbox', follow_activity_id='https://bridge.test/follow/1',
        status='accepted')
    db.activitypub_objects.create_published_activity_object(
        actor_username='alice', actor_url=ACTOR, community_actor_url=COMMUNITY,
        activity_id=f'{ACTOR}/activities/create/posts/11', object_id=OBJECT,
        kind='post', title='Topic', body_markdown='old', in_reply_to_object_id=None,
        discord_channel_id=10, discord_message_id=11)
    source = MessageRef('discord-primary', '111', '11')
    db.message_mappings.create_message_mapping(source_platform='discord', source_id='11',
        activity_id=f'{ACTOR}/activities/create/posts/11', object_id=OBJECT,
        actor_url=ACTOR, community_actor_url=COMMUNITY, discord_channel_id=10,
        discord_message_id=11, source_message=source)
    with db.immediate_session() as session:
        session.scalar(select(PublishedActivityObject)).source_external_container_id = '111'
    yield db
    db.close()


def row(db):
    """Read the retained reservation after a complete public worker action."""
    with db.session() as session:
        return session.scalar(select(UserActorContentMutation))


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['missing_canonical', 'sql_failure'])
async def test_failed_local_commit_keeps_frozen_render_with_backoff_then_recovers(world, fault):
    """Failure parks the local commit; repairing storage resumes it without render/POST."""
    db = world
    armed = {'enabled': fault == 'sql_failure'}
    if fault == 'sql_failure':
        canonical(db)

    def fail_commit(conn, cursor, statement, parameters, context, executemany):
        """Inject an actual SQL flush failure only in the canonical commit transaction."""
        if armed['enabled'] and statement.startswith('UPDATE published_activity_objects SET') and 'content_revision=' in statement:
            raise OperationalError('injected local commit failure', {}, RuntimeError('disk fault'))

    event.listen(db.engine, 'before_cursor_execute', fail_commit)
    gateway = Renderer()
    worker = UserActorDeliveryService(db, gateway, SimpleNamespace())
    await worker.submit_update(object_id=OBJECT, title='Topic', body_markdown='new',
        edited_timestamp=datetime(2026, 10, 25, tzinfo=timezone.utc))
    saved = row(db)
    assert saved.state == 'canonical_ready'
    assert saved.last_error and saved.next_attempt_at is not None
    assert saved.claim_token is None and saved.lease_expires_at is None
    assert saved.side_effect_classification == 'canonical_received'
    frozen = saved.canonical_activity_json
    assert json.loads(frozen)['object']['content'] == 'new'
    with db.session() as session:
        published = session.scalar(select(PublishedActivityObject))
        assert published.body_markdown == 'old' and published.content_revision == 1
        assert published.desired_kind == 'update'
        assert session.query(UserActorOutboundSource).count() == 0
    # An immediate worker tick cannot reclaim hot-loop work or render it again.
    assert await worker.drain_mutations(limit=1) == 0
    assert gateway.calls == 1 and row(db).attempt_count == 1
    armed['enabled'] = False
    if fault == 'missing_canonical':
        canonical(db)
    resumed = Database(str(db.engine.url))
    try:
        recovered = UserActorDeliveryService(resumed, gateway, SimpleNamespace())
        assert await recovered.drain_mutations(limit=1) == 0
        with resumed.immediate_session() as session:
            session.get(UserActorContentMutation, saved.id).next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert await recovered.drain_mutations(limit=1) == 1
        final = row(resumed)
        assert final.state == 'committed' and final.canonical_activity_json == frozen
        assert final.attempt_count == 2
        assert final.last_error is None
        with resumed.session() as session:
            published = session.scalar(select(PublishedActivityObject))
            assert published.body_markdown == 'new' and published.content_revision == 2
            assert session.query(UserActorOutboundSource).count() == 1
        assert gateway.calls == 1
    finally:
        resumed.close()


@pytest.mark.asyncio
async def test_active_canonical_commit_claim_cannot_be_stolen(world):
    """A second worker waits for the exact ready-owner lease before local recovery."""
    db = world
    db.content_mutations.record_desired_update(object_id=OBJECT, title='Topic',
        body_markdown='new', edited_timestamp=datetime(2026, 10, 25, tzinfo=timezone.utc))
    db.content_mutations.reserve_next(object_id=OBJECT)
    claim = db.content_mutations.claim_due(limit=1, stale_after_seconds=300)[0]
    gateway = Renderer()
    rendered = await gateway.update_content(SimpleNamespace(activity_id=claim.activity_id,
        ap_object_id=OBJECT, body_markdown='new'))
    assert db.content_mutations.record_canonical_result(claim.mutation_id, claim.claim_token, rendered.activity_json)
    assert db.content_mutations.claim_due(limit=1, stale_after_seconds=300) == []
    # Once the owner lease really expires, the next token fences its late result.
    with db.immediate_session() as session:
        session.get(UserActorContentMutation, claim.mutation_id).lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    replacement = db.content_mutations.claim_due(limit=1, stale_after_seconds=300)[0]
    db.content_mutations.record_retryable_failure(claim.mutation_id, claim.claim_token, 'stale owner')
    assert row(db).claim_token == replacement.claim_token


@pytest.mark.asyncio
async def test_revision_conflict_parks_saved_render_without_overwriting_newer_state(world):
    """A refused commit CAS records a durable error and never re-renders stale work."""
    canonical(world)

    class ConcurrentRenderer(Renderer):
        """Expose a storage revision conflict across the awaited renderer boundary."""
        async def update_content(self, request):
            """Simulate a newer persisted revision before this local commit begins."""
            result = await super().update_content(request)
            with world.immediate_session() as session:
                published = session.scalar(select(PublishedActivityObject))
                published.content_revision = 2
                published.body_markdown = 'newer state'
            return result

    renderer = ConcurrentRenderer()
    worker = UserActorDeliveryService(world, renderer, SimpleNamespace())
    await worker.submit_update(object_id=OBJECT, title='Topic', body_markdown='stale state',
        edited_timestamp=datetime(2026, 10, 25, tzinfo=timezone.utc))
    parked = row(world)
    assert parked.state == 'canonical_ready'
    assert 'revision or ownership changed' in parked.last_error
    assert parked.claim_token is None and parked.next_attempt_at is not None
    assert json.loads(parked.canonical_activity_json)['object']['content'] == 'stale state'
    assert await worker.drain_mutations() == 0 and renderer.calls == 1
    with world.session() as session:
        published = session.scalar(select(PublishedActivityObject))
        assert published.body_markdown == 'newer state' and published.content_revision == 2
        assert session.query(UserActorOutboundSource).count() == 0
