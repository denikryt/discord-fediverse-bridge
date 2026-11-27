"""Remote media remains bound to real canonical deliveries and revision cursors."""
from __future__ import annotations

from datetime import timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy import select

from src.models import CommunityDiscordTargetClaim, CommunityThreadGroup, CommunityMessageGroup, RemotePersonCommentPlacement
from src.community_sync.group_view_mutations import GroupViewMutationService
from tests.behavior import test_remote_person_comments as person_comment_scenarios
from src.remote_person_comment_delivery import BotCommentTransport
from src.community_sync.group_view_delivery import CommunityGroupViewDeliveryWorker
from src.remote_person_webhooks import RemotePersonWebhookTransport
from tests.behavior.test_cp39_community_delivery import _EditableThread, _EditableMessage, _FakeBot, _FakeForum, _Policy
from tests.behavior import test_remote_person_discord_delivery as person_scenarios
from tests.test_phase5_inbound_ap_shared_groups import (
    _database, _accepted_subscription, _community_runtime, _fake_bot,
    _fake_forum_channel, _fake_post_event, _fake_comment_event, _publish_gateway,
)
from support.runtime import build_test_policy_service

PNG = 'https://media.example/photo.png'
JPEG = 'https://media.example/new.jpg'


def _image_bytes(fmt: str) -> bytes:
    """Create a small valid raster image at the mocked HTTP edge."""
    buffer = BytesIO()
    Image.new('RGB', (3, 3)).save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.fixture
def media_edge(monkeypatch):
    """Replace only the untrusted remote image GET, not delivery/SQLite logic."""
    from src import federated_media
    calls = []

    async def fetch(url):
        calls.append(url)
        if url == PNG:
            return federated_media.DownloadedImage(_image_bytes('PNG'), 'png')
        if url == JPEG:
            return federated_media.DownloadedImage(_image_bytes('JPEG'), 'jpg')
        raise TimeoutError('image is unavailable')

    monkeypatch.setattr(federated_media, '_download_image', fetch)
    return calls


def _allow_files(channel):
    """Provide the effective Discord permission required for file upload."""
    channel.guild = SimpleNamespace(me=object(), id=getattr(getattr(channel, "guild", None), "id", None))
    channel.permissions_for = lambda _: SimpleNamespace(
        attach_files=True, view_channel=True, read_message_history=True,
        send_messages=True, manage_webhooks=True, create_public_threads=True,
        send_messages_in_threads=True,
    )


def _files(kwargs, key='files'):
    """Inspect upload data at the SDK boundary before streams are closed."""
    return [(f.filename, f.fp.read()) for f in kwargs.get(key, [])]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["post", "comment"])
async def test_community_two_targets_and_mutation_remove_images(tmp_path, media_edge, kind):
    """A Community image is sent once per destination; retained edits replace it."""
    db = _database(tmp_path)
    try:
        forums = {}
        sent = []
        threads = []
        messages = []
        for channel, thread_id in ((100, 200), (101, 201)):
            _accepted_subscription(db, channel_id=channel)
            forum = _fake_forum_channel(channel_id=channel, thread_id=thread_id, starter_message_id=thread_id)
            _allow_files(forum)
            thread = _EditableThread(thread_id, channel, 9, '')
            _allow_files(thread)
            threads.append(thread)
            async def create(*, _thread=thread, **kwargs):
                sent.append((kwargs['content'], kwargs['suppress_embeds'], _files(kwargs)))
                _thread._starter.content = kwargs['content']
                return _thread, _thread._starter
            forum.create_thread = AsyncMock(side_effect=create)
            reply = _EditableMessage(thread_id + 1000, thread_id, 9, '')
            thread.messages[reply.id] = reply
            async def send(body, *, _reply=reply, **kwargs):
                sent.append((body, kwargs['suppress_embeds'], _files(kwargs)))
                _reply.content = body
                return _reply
            thread.send = send
            messages.append(thread._starter if kind == 'post' else reply)
            forums[channel] = forum
        bot = _fake_bot(forum_channels=forums, threads={t.id: t for t in threads})
        runtime = _community_runtime(db, _publish_gateway(), bot=bot)
        context = SimpleNamespace(database=db, bot=bot, community_runtime=runtime,
            bridge_policy_service=build_test_policy_service(db))
        event = _fake_post_event()
        if kind == 'comment':
            assert (await runtime.handle_inbound_post(event, context)).status == 'processed'
            sent.clear()
            event = _fake_comment_event()
        create = runtime.handle_inbound_post if kind == 'post' else runtime.handle_inbound_comment
        event.object.body_markdown = f'![photo]({PNG}) [ordinary](https://example.org/article)'
        assert (await create(event, context)).status == 'processed'
        assert len(sent) == 2
        assert all(PNG in body and suppress and files == [('image-1.png', _image_bytes('PNG'))]
                   for body, suppress, files in sent)
        await create(event, context)
        assert len(sent) == 2
        assert set(media_edge) == {PNG}

        # A retained view revision must PATCH the original starter IDs, not POST.
        edits = []
        for message in messages:
            async def edit(*, _message=message, **kwargs):
                edits.append((_message.id, kwargs['content'], kwargs['suppress'], _files(kwargs, 'attachments')))
            message.edit = edit
        worker = CommunityGroupViewDeliveryWorker(database=db, bot=_FakeBot(threads), policy_service=_Policy())
        service = GroupViewMutationService(db, _Policy())
        for revision, state, body in ((1, 'live', 'new attachment'), (2, 'live', 'No photo'), (3, 'deleted', 'No photo')):
            operation = 'Delete' if state == 'deleted' else 'Update'
            activity_id = f'{event.object.ap_id}/activity/{revision}'
            note = {'id': event.object.ap_id, 'type': 'Note', 'attributedTo': event.actor_id,
                    'audience': event.community_actor_id, 'content': body}
            if kind == 'comment':
                note['inReplyTo'] = event.object.post_ap_id
            if revision == 1:
                note['attachment'] = [{'type': 'Image', 'url': JPEG, 'mediaType': 'image/jpeg'}]
            mutation = event.model_copy(deep=True, update={
                'event_type': f'{kind}.{"deleted" if state == "deleted" else "updated"}',
                'delivery_id': activity_id, 'source_activity_id': activity_id,
                'verified_sender_actor_id': event.actor_id,
                'source_activity_json': {'id': activity_id, 'type': operation, 'actor': event.actor_id,
                    'audience': event.community_actor_id,
                    'object': event.object.ap_id if state == 'deleted' else note}})
            mutation.object.body_markdown = body
            assert (await service.apply(mutation)).revision == revision
            assert await worker.drain_once() == 2
            assert {e[0] for e in edits[-2:]} == {m.id for m in messages}
            assert all(e[2] for e in edits[-2:])
            expected = [('image-1.jpg', _image_bytes('JPEG'))] if revision == 1 else []
            assert all(e[3] == expected for e in edits[-2:])
            if revision == 1:
                assert all(JPEG in e[1] for e in edits[-2:])
            assert (await service.apply(mutation)).status == 'duplicate'
        with db.session() as session:
            claims = session.scalars(select(CommunityDiscordTargetClaim).where(CommunityDiscordTargetClaim.kind == kind))
            assert {c.applied_revision for c in claims} == {3}
        assert len(sent) == 2
    finally:
        db.close()


@pytest.mark.parametrize("lost_ack", [False, True])
def test_person_root_media_retains_original_id_and_link(media_edge, lost_ack):
    """Real Person reservation/worker uses a webhook file on both retained copies."""
    fixture = person_scenarios.RemotePersonDiscordDeliveryScenarios()
    fixture.setUp()
    try:
        class Hook:
            """Record physical webhook requests, including file bytes and history."""
            def __init__(self, channel):
                self.id = channel.id + 100
                self.channel_id = channel.id
                self.type = 1
                self.user = SimpleNamespace(id=9)
                self.token = 'test-only'
                self.name = 'Fediverse Bridge'
                self.sent = []
                self.edits = []
                self.deleted = []
                self.channel = channel
                self.history = []
            async def send(self, **kwargs):
                self.sent.append((kwargs['content'], kwargs['suppress_embeds'], _files(kwargs)))
                message = SimpleNamespace(id=self.channel_id + 1000, webhook_id=self.id, content=kwargs['content'])
                self.history.append(message)
                if lost_ack and self.channel_id == 100:
                    raise TimeoutError('response lost after upload')
                return message
            async def edit_message(self, message_id, **kwargs):
                self.edits.append((message_id, kwargs['content'], _files(kwargs, 'attachments')))
            async def delete_message(self, message_id):
                self.deleted.append(message_id)

        channels = {}
        for cid in (100, 101):
            channel = SimpleNamespace(id=cid, type=0)
            _allow_files(channel)
            hook = Hook(channel)
            channel.hook = hook
            async def history(*, limit, _hook=hook):
                for message in _hook.history[:limit]:
                    yield message
            channel.history = history
            channel.webhooks = AsyncMock(return_value=[hook])
            async def create_thread(*, name, _cid=cid):
                return SimpleNamespace(id=_cid+1000)
            channel.fetch_message = AsyncMock(return_value=SimpleNamespace(create_thread=create_thread))
            channels[cid] = channel
        bot = SimpleNamespace(user=SimpleNamespace(id=9), get_channel=channels.get)
        fixture.worker.transport = RemotePersonWebhookTransport(bot)
        event = fixture.event()
        event.object.body_markdown = f'![photo]({PNG})'
        event.source_activity_json['object']['attachment'] = [{'type': 'Image', 'url': PNG, 'mediaType': 'image/png'}]
        fixture.inbound.reserve(event)
        fixture.run_drain()
        assert all(row[1] == 'delivered' for row in fixture.root_rows())
        for channel in channels.values():
            [(content, suppressed, files)] = channel.hook.sent
            assert content.count(PNG) == 1 and suppressed
            assert files == [('image-1.png', _image_bytes('PNG'))]
        fixture.inbound.reserve(event)
        fixture.run_drain()
        for revision, url in ((1, JPEG), (2, 'https://media.example/unavailable.png'), (3, None)):
            update = fixture.event('updated', when=fixture.when + timedelta(minutes=revision))
            update.object.body_markdown = f'![photo]({url})' if url else 'removed'
            update.source_activity_json['object']['attachment'] = [{'type': 'Image', 'url': url}] if url else []
            fixture.inbound.reserve(update)
            fixture.run_drain()
            for channel in channels.values():
                mid, content, files = channel.hook.edits[-1]
                assert mid == channel.id + 1000
                assert files == ([('image-1.jpg', _image_bytes('JPEG'))] if revision == 1 else [])
                if url:
                    assert content.count(url) == 1
        fixture.inbound.reserve(fixture.event('deleted', when=fixture.when+timedelta(minutes=4)))
        fixture.run_drain()
        assert all(len(c.hook.sent) == 1 and c.hook.deleted == [c.id+1000] for c in channels.values())
    finally:
        fixture.doCleanups()


@pytest.mark.parametrize('lost_ack', [False, True])
def test_person_reply_media_retains_exact_message_id(media_edge, lost_ack):
    """Native reply placements keep the original bot IDs through image edits."""
    import asyncio
    fixture = person_comment_scenarios.RemotePersonComments()
    fixture.setUp()
    try:
        fixture.deliver_roots()
        worker, _ = fixture.worker()
        threads = {}
        sends, edits, deletes = [], [], []
        for channel_id, guild_id in ((100, 1), (101, 2)):
            thread_id = 80000 + channel_id
            thread = _EditableThread(thread_id, channel_id, 9, '')
            thread.guild.id = guild_id
            _allow_files(thread)
            message = _EditableMessage(thread_id + 5000, thread_id, 9, '')
            thread.messages[message.id] = message
            async def send(content, *, _thread=thread, _message=message, **kwargs):
                sends.append((_message.id, content, kwargs['suppress_embeds'], _files(kwargs)))
                _message.content = content
                _thread._messages.append(_message)
                if lost_ack and _thread.parent_id == 100:
                    raise TimeoutError('response lost after upload')
                return _message
            async def edit(*, _message=message, **kwargs):
                edits.append((_message.id, kwargs['content'], kwargs['suppress'], _files(kwargs, 'attachments')))
            async def delete(*, _message=message):
                deletes.append(_message.id)
            thread.send = send
            message.edit, message.delete = edit, delete
            threads[thread_id] = thread
        worker.transport = BotCommentTransport(SimpleNamespace(user=SimpleNamespace(id=9), get_channel=threads.get))
        create = fixture.event()
        create.object.body_markdown = 'An HTML image'
        create.source_activity_json['object']['content'] = f'<p>An HTML image</p><img src="{PNG}">'
        fixture.comments.reserve(create)
        asyncio.run(worker.drain_once())
        assert len(sends) == 2
        assert all(PNG in c and s and f == [('image-1.png', _image_bytes('PNG'))] for _, c, s, f in sends)
        with fixture.db.session() as session:
            assert {p.state for p in session.scalars(select(RemotePersonCommentPlacement))} == {'delivered'}
        fixture.comments.reserve(create)
        asyncio.run(worker.drain_once())
        for revision, url in ((1, JPEG), (2, None)):
            update = fixture.event(operation='updated', when=fixture.when + timedelta(minutes=revision))
            update.delivery_id += f'/{revision}'
            update.source_activity_id = update.delivery_id
            update.source_activity_json['id'] = update.delivery_id
            update.object.body_markdown = 'Current reply'
            update.source_activity_json['object']['attachment'] = [{'type': 'Image', 'url': url}] if url else []
            fixture.comments.reserve(update)
            asyncio.run(worker.drain_once())
            assert {e[0] for e in edits[-2:]} == {s[0] for s in sends}
            assert all(e[2] and e[3] == ([('image-1.jpg', _image_bytes('JPEG'))] if url else []) for e in edits[-2:])
        fixture.comments.reserve(fixture.event(operation='deleted', when=fixture.when + timedelta(minutes=3)))
        asyncio.run(worker.drain_once())
        assert set(deletes) == {s[0] for s in sends}
        assert len(sends) == 2
    finally:
        fixture.doCleanups()


@pytest.mark.asyncio
async def test_community_lost_ack_with_image_adopts_original_message(tmp_path, media_edge):
    """A timed-out upload is reconciled by the unchanged canonical footer."""
    db = _database(tmp_path)
    try:
        _accepted_subscription(db, channel_id=100)
        thread = _EditableThread(200, 100, 9, '')
        _allow_files(thread)
        forum = _FakeForum([], forum_id=100)
        _allow_files(forum)
        sends = []
        async def create(**kwargs):
            sends.append(_files(kwargs))
            thread._starter.content = kwargs['content']
            forum.threads.append(thread)
            raise TimeoutError('accepted upload, lost response')
        forum.create_thread = create
        bot = _fake_bot(forum_channels={100: forum}, threads={200: thread})
        bot.user = SimpleNamespace(id=9)
        runtime = _community_runtime(db, _publish_gateway(), bot=bot)
        context = SimpleNamespace(database=db, bot=bot, community_runtime=runtime,
            bridge_policy_service=build_test_policy_service(db))
        event = _fake_post_event()
        event.object.body_markdown = f'![]({PNG})'
        await runtime.handle_inbound_post(event, context)
        with db.session() as session:
            assert session.scalar(select(CommunityDiscordTargetClaim)).status == 'unknown'
        await runtime.handle_inbound_post(event, context)
        assert sends == [[('image-1.png', _image_bytes('PNG'))]]
        worker = CommunityGroupViewDeliveryWorker(database=db, bot=bot, policy_service=_Policy())
        assert await worker.drain_once() == 1
        with db.session() as session:
            claim = session.scalar(select(CommunityDiscordTargetClaim))
            assert claim.status == 'completed' and claim.applied_revision == 0
        assert len(sends) == 1
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['update', 'delete'])
async def test_community_mutation_during_image_download_is_fenced(tmp_path, monkeypatch, change):
    """An image GET is preflight, so a newer revision owns the eventual POST."""
    from src import federated_media
    db = _database(tmp_path)
    try:
        _accepted_subscription(db, channel_id=100)
        forum = _fake_forum_channel(channel_id=100, thread_id=200, starter_message_id=200)
        _allow_files(forum)
        bot = _fake_bot(forum_channels={100: forum})
        runtime = _community_runtime(db, _publish_gateway(), bot=bot)
        context = SimpleNamespace(database=db, bot=bot, community_runtime=runtime,
            bridge_policy_service=build_test_policy_service(db))
        event = _fake_post_event()
        event.object.body_markdown = f'![photo]({PNG})'
        service = GroupViewMutationService(db, _Policy())
        posts = []
        async def send(**kwargs):
            posts.append((kwargs['content'], _files(kwargs)))
            return SimpleNamespace(id=200), SimpleNamespace(id=200)
        forum.create_thread = send
        async def fetch(url):
            if url == PNG:
                activity_id = event.object.ap_id + '/mutation'
                note = {'id': event.object.ap_id, 'type': 'Note', 'attributedTo': event.actor_id,
                        'audience': event.community_actor_id, 'content': f'![new]({JPEG})'}
                mutation = event.model_copy(deep=True, update={
                    'event_type': 'post.updated' if change == 'update' else 'post.deleted',
                    'delivery_id': activity_id, 'source_activity_id': activity_id,
                    'verified_sender_actor_id': event.actor_id,
                    'source_activity_json': {'id': activity_id, 'type': 'Update' if change == 'update' else 'Delete',
                        'actor': event.actor_id, 'audience': event.community_actor_id,
                        'object': note if change == 'update' else event.object.ap_id}})
                mutation.object.body_markdown = f'![new]({JPEG})'
                assert (await service.apply(mutation)).revision == 1
                return federated_media.DownloadedImage(_image_bytes('PNG'), 'png')
            assert url == JPEG
            return federated_media.DownloadedImage(_image_bytes('JPEG'), 'jpg')
        monkeypatch.setattr(federated_media, '_download_image', fetch)
        await runtime.handle_inbound_post(event, context)
        if change == 'delete':
            assert posts == []
        else:
            assert len(posts) == 1 and JPEG in posts[0][0] and PNG not in posts[0][0]
            assert posts[0][1] == [('image-1.jpg', _image_bytes('JPEG'))]
            with db.session() as session:
                assert session.scalar(select(CommunityDiscordTargetClaim)).applied_revision == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_nodebb_verified_note_html_and_markdown_reach_discord(tmp_path, monkeypatch, media_edge):
    """NodeBB Group Announce URI dereference preserves image-only HTML sources."""
    from tests.behavior import test_nodebb_group_federation_scenarios as nodebb
    from src.community_sync import nodebb_inbound
    db, runtime, forum, thread = nodebb._runtime(tmp_path)
    try:
        _allow_files(forum)
        _allow_files(thread)
        docs = {nodebb.GROUP: nodebb._fixture('category_actor.json'),
                nodebb.ROOT: nodebb._fixture('root_note.json'),
                nodebb.REPLY: nodebb._fixture('reply_note.json')}
        docs[nodebb.ROOT]['source'] = {'content': f'![photo]({PNG})', 'mediaType': 'text/markdown'}
        docs[nodebb.REPLY].pop('source', None)
        docs[nodebb.REPLY]['content'] = f'<p>Reply</p><img src="{JPEG}">'
        class Fetch:
            """Serve verified NodeBB documents at the HTTPS boundary."""
            def __init__(self, *_):
                pass
            async def __call__(self, url, _accept):
                return docs[url]
        monkeypatch.setattr(nodebb_inbound, 'SecureNodeBBJSON', Fetch)
        root_uploads, comment_uploads = [], []
        async def create(**kwargs):
            root_uploads.append(_files(kwargs))
            return SimpleNamespace(id=200), SimpleNamespace(id=300)
        async def send(body, **kwargs):
            assert JPEG in body
            comment_uploads.append(_files(kwargs))
            return SimpleNamespace(id=400)
        forum.create_thread = create
        thread.send = send
        assert (await nodebb_inbound.handle_nodebb_announcement(nodebb._event('announce_root_uri.json'), runtime)).status == 'processed'
        assert (await nodebb_inbound.handle_nodebb_announcement(nodebb._event('announce_reply_uri.json'), runtime)).status == 'processed'
        assert root_uploads == [[('image-1.png', _image_bytes('PNG'))]]
        assert comment_uploads == [[('image-1.jpg', _image_bytes('JPEG'))]]
        with db.session() as session:
            group = session.scalar(select(CommunityMessageGroup))
            assert JPEG in group.display_object_json['media_source']['content']
    finally:
        db.close()


@pytest.mark.asyncio
async def test_failed_image_first_forum_does_not_block_second(tmp_path, monkeypatch):
    """A failed image GET is text-only delivery, not a failed target claim."""
    from src import federated_media
    db = _database(tmp_path)
    try:
        sent = []
        forums = {}
        for cid in (100, 101):
            _accepted_subscription(db, channel_id=cid)
            forum = _fake_forum_channel(channel_id=cid, thread_id=cid+100, starter_message_id=cid+100)
            _allow_files(forum)
            async def create(*, _cid=cid, **kwargs):
                sent.append((_cid, kwargs['content'], _files(kwargs)))
                return SimpleNamespace(id=_cid+100), SimpleNamespace(id=_cid+100)
            forum.create_thread = create
            forums[cid] = forum
        calls = []
        async def fetch(url):
            calls.append(url)
            if len(calls) == 1:
                raise TimeoutError('first download failed')
            return federated_media.DownloadedImage(_image_bytes('PNG'), 'png')
        monkeypatch.setattr(federated_media, '_download_image', fetch)
        bot = _fake_bot(forum_channels=forums)
        runtime = _community_runtime(db, _publish_gateway(), bot=bot)
        context = SimpleNamespace(database=db, bot=bot, community_runtime=runtime,
            bridge_policy_service=build_test_policy_service(db))
        event = _fake_post_event()
        event.object.body_markdown = f'![photo]({PNG})'
        assert (await runtime.handle_inbound_post(event, context)).status == 'processed'
        assert len(sent) == 2 and all(PNG in c for _, c, _ in sent)
        assert sent[0][2] == [] and sent[1][2] == [('image-1.png', _image_bytes('PNG'))]
        with db.session() as session:
            assert {c.status for c in session.scalars(select(CommunityDiscordTargetClaim))} == {'completed'}
    finally:
        db.close()


def test_person_reply_root_delete_during_download_blocks_new_post(monkeypatch):
    """The new asynchronous media preflight cannot outlive a deleted root."""
    import asyncio
    from src import federated_media
    from src.models import RemotePersonPost
    fixture = person_comment_scenarios.RemotePersonComments()
    fixture.setUp()
    try:
        fixture.deliver_roots()
        event = fixture.event()
        event.object.body_markdown = f'![image]({PNG})'
        fixture.comments.reserve(event)
        worker, edge = fixture.worker()
        async def fetch(_url):
            from src.activitypub_models import RemotePersonContentEvent
            from src.remote_person_inbound import PUBLIC
            actor, obj = person_comment_scenarios.BOB, person_comment_scenarios.NOTE
            source = {'id': obj+'/delete', 'type': 'Delete', 'actor': actor,
                      'to': [PUBLIC], 'object': obj}
            fixture.roots.reserve(RemotePersonContentEvent(event_type='remote.person.deleted',
                delivery_id=source['id'], occurred_at=fixture.when+timedelta(minutes=1),
                actor_id=actor, verified_sender_actor_id=actor,
                source_activity_id=source['id'], source_activity_json=source,
                object={'ap_id': obj, 'author_name': 'bob'}))
            with fixture.db.session() as session:
                assert session.scalar(select(RemotePersonPost)).deleted_at is not None
            return federated_media.DownloadedImage(_image_bytes('PNG'), 'png')
        monkeypatch.setattr(federated_media, '_download_image', fetch)
        asyncio.run(worker.drain_once())
        assert edge.calls == []
        with fixture.db.session() as session:
            assert {row.state for row in session.scalars(select(RemotePersonCommentPlacement))} == {'suppressed'}
    finally:
        fixture.doCleanups()
