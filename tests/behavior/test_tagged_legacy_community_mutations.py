"""First mutations of authentic tagged-v0 data preserve humans and durable ACKs."""
from __future__ import annotations

import asyncio
import io
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.activitypub_models import ActivityPubEvent
from src.community_sync.runtime import CommunityRuntime
from src.content_sync.platform_mutations import PlatformPhysicalMutationCoordinator
from src.db import Database
from src.local_communities.mutations import LocalCommunityMutationHandler
from src.models import (
    CommunityThreadGroup, CommunityMessageGroup, LocalCommunityThread,
    LocalCommunityMessage, LocalCommunityMessageSurface, LocalCommunityThreadSurface,
    CommunityThreadGroupDelivery, CommunityMessageGroupDelivery, PublishedActivityObject,
)
from src.platform_types import AuthorRef, ContentDocument, MessageView

ACTOR = 'https://bridge.test/actors/users/alice'
MODELS = {('remote', 'post'): CommunityThreadGroup, ('remote', 'comment'): CommunityMessageGroup,
          ('local', 'post'): LocalCommunityThread, ('local', 'comment'): LocalCommunityMessage}
IDS = {('remote', 'post'): 30, ('remote', 'comment'): 33,
       ('local', 'post'): 40, ('local', 'comment'): 43}
COPIES = {('remote', 'post'): CommunityThreadGroupDelivery,
          ('remote', 'comment'): CommunityMessageGroupDelivery,
          ('local', 'post'): LocalCommunityThreadSurface,
          ('local', 'comment'): LocalCommunityMessageSurface}


@pytest.fixture(scope='module')
def tagged_v0(tmp_path_factory):
    """Execute released Database.create_all and ORM in a separate Python process."""
    repo = Path(__file__).resolve().parents[2]
    root = tmp_path_factory.mktemp('tagged-v0')
    archive = subprocess.run(['git', 'archive', 'v0.1.0', 'src'], cwd=repo,
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        bundle.extractall(root, filter='data')
    target = root / 'seed.sqlite'
    # The subprocess imports src exclusively from the release, with current dependencies.
    seed = repo / 'tests/support/seed_tagged_v0_database.py'
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join([str(root)] + [str(Path(p).resolve()) for p in
                                      env.get('PYTHONPATH', '').split(os.pathsep) if p])
    subprocess.run([sys.executable, '-c',
                    'import runpy,sys; sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name="__main__")',
                    str(seed), str(target)], cwd=root, env=env, check=True, capture_output=True)
    return target


@pytest.fixture
def migrated(tagged_v0, tmp_path):
    """Upgrade a fresh real release database through the public startup migrator."""
    path = tmp_path / 'bridge.sqlite'
    shutil.copyfile(tagged_v0, path)
    db = Database(f'sqlite:///{path}')
    db.migrate()
    yield db
    db.close()


class Transport:
    """Model independent ownership reads and possible loss of an edit ACK."""
    def __init__(self, *, fail=False, unknown=False, subscriber_source=False):
        """Keep physical outcomes and verified ownership independently controllable."""
        self.calls = []
        self.fail = fail
        self.unknown = unknown
        self.subscriber_source = subscriber_source

    async def fetch_message(self, message):
        """Return bridge ownership only for the exact bot-created mirror."""
        human = message.external_container_id in ('1000', '3000', '3300')
        if self.subscriber_source:
            human = message.external_container_id == '3100'
        owned = not human and not self.unknown
        return MessageView(message, AuthorRef(message.connector_key,
            'bot' if owned else 'human', 'bot' if owned else 'human', 'Actor', owned),
            ContentDocument('previous body'), None, datetime.now(timezone.utc))

    async def edit_message(self, message, content):
        """Record the physical side effect before optionally dropping its ACK."""
        self.calls.append(('edit', message, content.body))
        if self.fail:
            raise ConnectionError('lost ACK')

    async def delete_message(self, message):
        """Record a bot copy deletion independently of the canonical tombstone."""
        self.calls.append(('delete', message, None))


def worker(db, transport):
    """Bind real SQLite claim/settlement to the outer transport boundary."""
    return PlatformPhysicalMutationCoordinator(db, SimpleNamespace(get=lambda key: transport),
        target_policy=lambda key, surface: True,
        content_renderer=lambda domain, kind, data: ContentDocument(data['bodyMarkdown']))


def event(domain, kind, row, *, operation='updated', actor=ACTOR):
    """Supply an authenticated gateway mutation without manufacturing an old snapshot."""
    return ActivityPubEvent.model_validate(dict(event_type=f'{kind}.{operation}',
        delivery_id='delivery/1', source_activity_id='https://bridge.test/update/1',
        occurred_at='2026-10-25T10:00:00Z',
        community_actor_id=('https://remote.test/c/news' if domain == 'remote'
                            else 'https://bridge.test/actors/communities/news'), actor_id=actor,
        object=dict(ap_id=row.ap_object_id, kind=kind, lemmy_id=1, title='Changed',
                    body_markdown='latest', published_at='2025-01-01T00:00:00Z',
                    updated_at='2026-10-25T10:00:00Z', author_name='Alice')))


async def dispatch(db, domain, kind, row, transport, *, operation='updated', actor=ACTOR):
    """Enter the actual Local/Remote AP handlers, faking only network relay."""
    runtime = SimpleNamespace(physical_mutation_coordinator=worker(db, transport))
    if domain == 'remote':
        handler = CommunityRuntime(database=db, content_publish_service=object())
    else:
        class Relay:
            """Replace the outbound federation edge only."""
            async def relay_update_or_delete(self, **kwargs):
                """No live inbox exists in this database scenario."""
        handler = LocalCommunityMutationHandler(database=db, fedify_gateway=None,
            bridge_policy_service=None, surface_fanout=None, federation_fanout=Relay(),
            is_community_id_disabled=lambda ident: False)
    method = getattr(handler, f'handle_inbound_{kind}_{"update" if operation == "updated" else "delete"}')
    return await method(event(domain, kind, row, operation=operation, actor=actor), runtime)


@pytest.mark.parametrize('domain,kind', IDS)
@pytest.mark.parametrize('operation', ['updated', 'deleted'])
def test_first_legacy_mutation_through_handler_then_restart(migrated, domain, kind, operation):
    """A proven first mutation reaches only the mirror, then restart sends nothing."""
    db = migrated
    with db.session() as session:
        row = session.get(MODELS[domain, kind], IDS[domain, kind])
        assert row.current_content_json is None and row.content_revision == 0
    forged = Transport()
    result = asyncio.run(dispatch(db, domain, kind, row, forged, operation=operation, actor='https://forged/actor'))
    assert result.status == 'skipped' and not forged.calls
    transport = Transport()
    result = asyncio.run(dispatch(db, domain, kind, row, transport, operation=operation))
    assert result.status == 'processed', result.detail
    assert len(transport.calls) == 1
    assert transport.calls[0][1].external_container_id in ('2000', '3100')
    with db.session() as session:
        current = session.get(MODELS[domain, kind], row.id)
        assert current.content_revision == 1
        assert (current.deleted_at is not None) == (operation == 'deleted')
        if operation == 'updated':
            assert json.loads(current.current_content_json)['bodyMarkdown'] == 'latest'
        assert session.query(COPIES[domain, kind]).filter_by(applied_revision=1).count() == 1
    resumed = Database(str(db.engine.url))
    try:
        resumed.migrate()
        second = Transport()
        assert asyncio.run(worker(resumed, second).drain()) == 0
        assert not second.calls
    finally:
        resumed.close()


@pytest.mark.parametrize('domain,kind', IDS)
@pytest.mark.parametrize('operation', ['updated', 'deleted'])
def test_missing_independent_author_evidence_refuses_first_update(migrated, domain, kind, operation):
    """Actor claims in the new event cannot substitute for original ownership."""
    with migrated.immediate_session() as session:
        row = session.get(MODELS[domain, kind], IDS[domain, kind])
        session.query(PublishedActivityObject).filter_by(object_id=row.ap_object_id).delete()
    transport = Transport()
    result = asyncio.run(dispatch(migrated, domain, kind, row, transport, operation=operation))
    assert result.status == 'skipped' and 'provenance' in result.detail.lower()
    assert not transport.calls
    with migrated.session() as session:
        assert session.get(MODELS[domain, kind], row.id).content_revision == 0


@pytest.mark.parametrize('domain,kind', IDS)
def test_legacy_lost_ack_remains_unresolved_after_restart(migrated, domain, kind):
    """First admission plus an uncertain mirror edit never permits a second write."""
    with migrated.session() as session:
        row = session.get(MODELS[domain, kind], IDS[domain, kind])
    transport = Transport(fail=True)
    assert asyncio.run(dispatch(migrated, domain, kind, row, transport)).status == 'processed'
    assert len(transport.calls) == 1
    with migrated.session() as session:
        assert session.query(COPIES[domain, kind]).filter_by(mutation_state='unresolved').count() == 1
    resumed = Database(str(migrated.engine.url))
    try:
        second = Transport()
        asyncio.run(worker(resumed, second).drain())
        assert second.calls == []
    finally:
        resumed.close()


def test_ambiguous_legacy_comment_copies_require_positive_ownership(migrated):
    """Missing typed source identity cannot authorize either unknown physical copy."""
    with migrated.session() as session:
        row = session.get(LocalCommunityMessage, 43)
        assert row.source_external_message_id is None
    transport = Transport(unknown=True)
    assert asyncio.run(dispatch(migrated, 'local', 'comment', row, transport)).status == 'processed'
    assert transport.calls == []
    with migrated.session() as session:
        copies = session.scalars(select(LocalCommunityMessageSurface).where(
            LocalCommunityMessageSurface.local_community_message_id == row.id)).all()
        assert all(c.applied_revision is None and c.mutation_state == 'failed_definite' for c in copies)
        assert all('ownership' in c.mutation_last_error.lower() for c in copies)


@pytest.mark.parametrize('ident,subscriber_source,expected', [(43, False, '3100'), (44, True, '3000')])
def test_host_and_subscriber_nested_sources_are_never_mutated(migrated, ident, subscriber_source, expected):
    """Read ownership for each ambiguous legacy comment, including nested replies."""
    with migrated.session() as session:
        row = session.get(LocalCommunityMessage, ident)
        assert row.source_external_message_id is None
    transport = Transport(subscriber_source=subscriber_source)
    assert asyncio.run(dispatch(migrated, 'local', 'comment', row, transport)).status == 'processed'
    assert len(transport.calls) == 1
    assert transport.calls[0][1].external_container_id == expected
    with migrated.session() as session:
        copies = session.scalars(select(LocalCommunityMessageSurface).where(
            LocalCommunityMessageSurface.local_community_message_id == ident)).all()
        assert sorted(c.mutation_state for c in copies) == ['failed_definite', 'idle']
        assert sum(c.applied_revision == 1 for c in copies) == 1


@pytest.mark.parametrize('kind,ident', [('post', 60), ('comment', 61)])
@pytest.mark.parametrize('operation', ['updated', 'deleted'])
def test_original_inbound_create_proves_legacy_author(migrated, kind, ident, operation):
    """An original migrated relay Create proves author independently of the new event."""
    with migrated.session() as session:
        row = session.get(MODELS['local', kind], ident)
        assert row.current_content_json is None
    transport = Transport()
    wrong = asyncio.run(dispatch(migrated, 'local', kind, row, transport, operation=operation))
    assert wrong.status == 'skipped'
    result = asyncio.run(dispatch(migrated, 'local', kind, row, transport,
        operation=operation, actor='https://remote.test/u/original'))
    assert result.status == 'processed', result.detail
    assert not transport.calls
    with migrated.session() as session:
        current = session.get(MODELS['local', kind], ident)
        assert current.content_revision == 1
        assert (current.deleted_at is not None) == (operation == 'deleted')


@pytest.mark.parametrize('field,value', [
    ('activity_id', 'https://other/activity'), ('community_actor_url', 'https://other/community'),
    ('kind', 'comment'), ('actor_url', 'https://wrong/author'),
])
def test_conflicting_original_publication_cannot_bootstrap(migrated, field, value):
    """Exact original activity, kind, community and actor all bind first admission."""
    with migrated.immediate_session() as session:
        row = session.get(LocalCommunityThread, 40)
        published = session.scalar(select(PublishedActivityObject).where(
            PublishedActivityObject.object_id == row.ap_object_id))
        setattr(published, field, value)
    transport = Transport()
    result = asyncio.run(dispatch(migrated, 'local', 'post', row, transport))
    assert result.status == 'skipped' and not transport.calls
    with migrated.session() as session:
        assert session.get(LocalCommunityThread, row.id).content_revision == 0


@pytest.mark.parametrize('operation', ['updated', 'deleted'])
def test_subscriber_origin_legacy_post_preserves_human_starter(migrated, operation):
    """A role=host receipt is a bot mirror when the author posted in a subscriber."""
    with migrated.session() as session:
        row = session.get(LocalCommunityThread, 62)
        assert row.source_external_thread_id is None
    transport = Transport()
    result = asyncio.run(dispatch(migrated, 'local', 'post', row, transport, operation=operation))
    assert result.status == 'processed'
    assert len(transport.calls) == 1
    assert transport.calls[0][1].external_container_id == '3200'
    with migrated.session() as session:
        assert session.get(LocalCommunityThreadSurface, 64).applied_revision is None
        assert session.get(LocalCommunityThreadSurface, 64).mutation_state == 'failed_definite'
        assert session.get(LocalCommunityThreadSurface, 63).applied_revision == 1
