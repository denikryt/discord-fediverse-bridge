"""User Actor follow notices and Discord reconciliation through physical adapter."""

from __future__ import annotations

from datetime import datetime, timezone
import sys
from types import ModuleType
from types import SimpleNamespace

import pytest

from src.discord_adapter import DiscordAdapter
from src.platform_types import ConnectorRef, PlatformOperationError
from src.db import Database
from src.user_actor_followers import UserActorFollowService


class NotFound(Exception):
    """Represent a Discord 404 at the outer SDK boundary."""


class _FakeChannel:
    """Expose only an exact physical Discord message and its observed channel."""

    def __init__(self, *, channel_id=10, message_id=11, message=None):
        """Create one text channel with a known message, if any."""
        self.id = channel_id
        self.guild = SimpleNamespace(id=2)
        self.name = 'source'
        self._message_id = message_id
        self.message = message
        self.fail = None

    def is_news(self):
        """Classify this physical channel as text."""
        return False

    async def fetch_message(self, message_id):
        """Return exact message or the configured transient/404 outcome."""
        if self.fail:
            raise self.fail
        if message_id != self._message_id or self.message is None:
            raise NotFound('message absent')
        return self.message


class _FakeUser:
    """Capture exact owner notice attempts through the borrowed Discord client."""

    def __init__(self, user_id):
        """Create one Discord DM target."""
        self.id = user_id
        self.messages = []
        self.fail = False

    async def send(self, content, **kwargs):
        """Record at-most-once physical POST including suppressed mentions."""
        self.messages.append((content, kwargs))
        if self.fail:
            raise TimeoutError('response lost after Discord accepted DM')


class _Bot:
    """Borrowed Discord SDK client with one owner and one channel."""

    def __init__(self, channel, user):
        """Expose cache reads and trusted SDK fetches."""
        self.channel = channel
        self.target_user = user
        self.user = SimpleNamespace(id=999)
        self.fetch_users = []

    def get_channel(self, channel_id):
        """Expose the configured cached channel only."""
        return self.channel if self.channel and channel_id == self.channel.id else None

    async def fetch_channel(self, channel_id):
        """Fail rather than inventing a channel on cache miss."""
        if self.channel is None or channel_id != self.channel.id:
            raise NotFound('channel absent')
        return self.channel

    async def fetch_user(self, user_id):
        """Return the requested actual registered Discord user."""
        self.fetch_users.append(user_id)
        return self.target_user


def _adapter(monkeypatch, channel, user):
    """Install an SDK edge fake but keep the production adapter and database real."""
    class _Discord:
        """Provide only SDK types this physical boundary needs."""

        TextChannel = _FakeChannel
        ForumChannel = type('ForumChannel', (), {})
        Thread = type('Thread', (), {})

        class AllowedMentions:
            """Track mention suppression explicitly."""

            @staticmethod
            def none():
                """Return an observable sentinel."""
                return 'no-mentions'

    monkeypatch.setattr(DiscordAdapter, '_sdk', staticmethod(lambda: _Discord))
    return DiscordAdapter(connector=ConnectorRef('discord-primary', 'discord'),
                          bot=_Bot(channel, user))


@pytest.mark.asyncio
async def test_reconciliation_adapter_returns_neutral_exact_state_and_not_found(monkeypatch):
    """An exact Discord edit yields primitive state; missing message means delete."""
    edited = datetime(2026, 10, 19, tzinfo=timezone.utc)
    channel = _FakeChannel(message=SimpleNamespace(
        id=11, channel=SimpleNamespace(id=10), content='edited', edited_at=edited,
        attachments=[],
    ))
    transport = _adapter(monkeypatch, channel, _FakeUser(42))

    status, state = await transport.fetch_message_state(10, 11)
    assert status == 'present'
    assert (state.content, state.edited_at) == ('edited', edited)
    assert not hasattr(state, 'channel')  # No SDK object escapes into User Actor.

    channel.message = None
    assert await transport.fetch_message_state(10, 11) == ('missing', None)


@pytest.mark.asyncio
async def test_reconciliation_adapter_rejects_wrong_receipt_and_transient_error(monkeypatch):
    """A wrong physical ID or transient fetch failure cannot synthesize Delete."""
    channel = _FakeChannel(message=SimpleNamespace(
        id=99, channel=SimpleNamespace(id=10), content='not the message',
        edited_at=datetime.now(timezone.utc), attachments=[],
    ))
    transport = _adapter(monkeypatch, channel, _FakeUser(42))

    with pytest.raises(PlatformOperationError) as wrong:
        await transport.fetch_message_state(10, 11)
    assert wrong.value.reason == 'invalid_request'

    channel.fail = TimeoutError('Discord temporarily unavailable')
    with pytest.raises(PlatformOperationError) as unavailable:
        await transport.fetch_message_state(10, 11)
    assert unavailable.value.reason == 'unavailable'


@pytest.mark.asyncio
async def test_discord_owner_notice_uses_registered_user_and_suppresses_mentions(monkeypatch):
    """Follow notification is an exact Discord-only DM, never a generic send."""
    target = _FakeUser(42)
    transport = _adapter(monkeypatch, None, target)
    await transport.send_owner_notice(42, '@everyone followed you')
    assert transport._bot.fetch_users == [42]
    assert target.messages == [('@everyone followed you', {'allowed_mentions': 'no-mentions'})]

    with pytest.raises(PlatformOperationError):
        await transport.send_owner_notice(0, 'invalid')
    assert len(target.messages) == 1


@pytest.mark.asyncio
async def test_accepted_follow_notification_remains_at_most_once_when_dm_reply_lost(tmp_path, monkeypatch):
    """Accepted Follow persists one notice claim before any uncertain Discord DM."""
    db = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    db.migrate()
    user = db.users.create_user(
        discord_user_id='42', activitypub_username='alice',
        actor_url='https://bridge.example/actors/users/alice',
        inbox_url='https://bridge.example/actors/users/alice/inbox',
        outbox_url='https://bridge.example/actors/users/alice/outbox',
        followers_url='https://bridge.example/actors/users/alice/followers',
        public_key_pem='public', private_key_pem='private',
    )

    class Gateway:
        """Accept Follow through the existing outer ActivityPub boundary."""

        async def accept_user_actor_follow(self, **_kwargs):
            """Model a successful signed Accept response."""

    class Notice:
        """One physical DM attempt that loses its HTTP response."""

        attempts = 0

        async def send_owner_notice(self, user_id, message):
            """Simulate Discord-side success then an ambiguous response."""
            assert user_id == 42 and 'alice' in message
            self.attempts += 1
            raise TimeoutError('DM created, response lost')

    # Legacy imports transitively load the Discord command UI. Supply an
    # import-only bot type for this SQLite/Fedify scenario; no SDK calls run.
    sdk_stub = ModuleType('discord')
    bot_module = ModuleType('src.discord_bot')
    bot_module.BridgeBot = type('BridgeBot', (), {})
    monkeypatch.setitem(sys.modules, 'discord', sdk_stub)
    monkeypatch.setitem(sys.modules, 'src.discord_bot', bot_module)
    from src.activitypub_models import LocalFollowRequestEvent
    from src.activitypub_handlers import dispatch_activitypub_event
    follower_id = 'https://remote.example/users/bob'
    notice = Notice()
    service = UserActorFollowService(database=db, fedify_gateway=Gateway(),
                                     discord_notices=notice)
    runtime = SimpleNamespace(database=db, user_actor_follow_service=service,
        bridge_policy_service=SimpleNamespace(
            federation_decision=lambda _: SimpleNamespace(allowed=True)))
    for follow_id in ('https://remote.example/follow/1', 'https://remote.example/follow/2'):
        event = LocalFollowRequestEvent(
            event_type='local.follow_requested', delivery_id=follow_id,
            occurred_at=datetime.now(timezone.utc), target_actor_kind='user',
            target_actor_id=user.actor_url, actor_id=follower_id,
            object={'follow_activity_id': follow_id,
                    'remote_inbox_url': f'{follower_id}/inbox',
                    'remote_shared_inbox_url': None},
        )
        result = await dispatch_activitypub_event(event, runtime)
        assert result.status == 'processed'

    assert notice.attempts == 1
    row = db.user_actor_followers.get_relationship(user_id=user.id, remote_actor_id=follower_id)
    assert row.owner_notification_state == 'attempted'
    db.close()


@pytest.mark.asyncio
async def test_user_actor_durable_reconciliation_uses_adapter_without_sdk_leak(tmp_path, monkeypatch):
    """One persisted partial edit resumes from the exact Discord state, not a bot."""
    from src.models import PublishedActivityObject
    from sqlalchemy import select
    from src.user_actor_delivery import UserActorDeliveryService
    from src.fedify_gateway_client import CanonicalMutationResult

    db = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    db.migrate()
    user = db.users.create_user(
        discord_user_id='42', activitypub_username='alice',
        actor_url='https://bridge.example/actors/users/alice',
        inbox_url='https://bridge.example/actors/users/alice/inbox',
        outbox_url='https://bridge.example/actors/users/alice/outbox',
        followers_url='https://bridge.example/actors/users/alice/followers',
        public_key_pem='public', private_key_pem='private',
    )
    object_id = 'https://bridge.example/posts/11'
    db.activitypub_objects.create_published_activity_object(
        actor_username='alice', actor_url=user.actor_url,
        community_actor_url='https://remote.example/c/community',
        activity_id='https://bridge.example/actors/users/alice/activities/create/11',
        object_id=object_id, kind='post', title=None,
        body_markdown='old', in_reply_to_object_id=None,
        discord_channel_id=10, discord_message_id=11,
    )
    db.content_mutations.request_discord_reconciliation(
        channel_id=10, message_id=11, error='partial gateway update',
    )

    edited = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)
    physical = SimpleNamespace(id=11, channel=SimpleNamespace(id=10),
                               content='new', edited_at=edited, attachments=[])
    channel = _FakeChannel(message=physical)
    transport = _adapter(monkeypatch, channel, _FakeUser(42))

    class Gateway:
        """Record signed update only after durable reconciliation is persisted."""
        updates = 0

        async def update_content(self, request):
            """Produce one canonical response from the persisted update claim."""
            self.updates += 1
            return CanonicalMutationResult(
                request.activity_id, request.ap_object_id,
                {'id': request.activity_id, 'type': 'Update', 'actor': user.actor_url,
                 'object': request.ap_object_id},
            )

        async def send_user_actor_deliveries(self, **_kwargs):
            """No accepted followers in this scenario."""
            return []

    policy = SimpleNamespace(federation_decision=lambda _: SimpleNamespace(allowed=True))
    gateway = Gateway()
    delivery = UserActorDeliveryService(db, gateway, policy,
                                       discord_message_reader=transport)
    await delivery.drain_once()
    with db.session() as session:
        published = session.scalar(select(PublishedActivityObject))
        assert published.body_markdown == 'new'
        assert published.discord_reconciliation_needed is False
        assert published.content_revision == 2
    assert gateway.updates == 1
    db.close()


@pytest.mark.asyncio
async def test_user_actor_durable_reconciliation_retries_transient_fetch(tmp_path, monkeypatch):
    """An unavailable Discord GET must not commit a forged ActivityPub Delete."""
    from src.models import PublishedActivityObject
    from sqlalchemy import select
    from src.user_actor_delivery import UserActorDeliveryService

    db = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    db.migrate()
    user = db.users.create_user(
        discord_user_id='42', activitypub_username='alice',
        actor_url='https://bridge.example/actors/users/alice',
        inbox_url='https://bridge.example/actors/users/alice/inbox',
        outbox_url='https://bridge.example/actors/users/alice/outbox',
        followers_url='https://bridge.example/actors/users/alice/followers',
        public_key_pem='public', private_key_pem='private',
    )
    db.activitypub_objects.create_published_activity_object(
        actor_username='alice', actor_url=user.actor_url,
        community_actor_url='https://remote.example/c/community',
        activity_id='https://bridge.example/actors/users/alice/activities/create/11',
        object_id='https://bridge.example/posts/11', kind='post', title=None,
        body_markdown='old', in_reply_to_object_id=None,
        discord_channel_id=10, discord_message_id=11,
    )
    db.content_mutations.request_discord_reconciliation(
        channel_id=10, message_id=11, error='partial edit',
    )
    channel = _FakeChannel()
    channel.fail = TimeoutError('Discord down')
    transport = _adapter(monkeypatch, channel, _FakeUser(42))

    class NeverSend:
        """Any ActivityPub mutation would indicate a fabricated Discord decision."""

        async def update_content(self, _request):
            """Fail if reconciliation incorrectly emits Update."""
            raise AssertionError('unexpected update')

        async def delete_content(self, _request):
            """Fail if reconciliation incorrectly emits Delete."""
            raise AssertionError('unexpected delete')

    delivery = UserActorDeliveryService(
        db, NeverSend(), SimpleNamespace(federation_decision=lambda _: SimpleNamespace(allowed=True)),
        discord_message_reader=transport,
    )
    await delivery.drain_reconciliations()
    with db.session() as session:
        published = session.scalar(select(PublishedActivityObject))
        assert published.body_markdown == 'old'
        assert published.content_revision == 1
        assert published.discord_reconciliation_needed is True
    db.close()


@pytest.mark.asyncio
async def test_discord_notice_refuses_wrong_fetched_user_and_unknown_delivery(monkeypatch):
    """An inconsistent Discord user lookup never sends, and an uncertain DM is not retried."""
    wrong = _FakeUser(99)
    transport = _adapter(monkeypatch, None, wrong)
    with pytest.raises(PlatformOperationError) as mismatch:
        await transport.send_owner_notice(42, 'follower arrived')
    assert mismatch.value.reason == 'invalid_request'
    assert wrong.messages == []

    owner = _FakeUser(42)
    owner.fail = True
    transport = _adapter(monkeypatch, None, owner)
    with pytest.raises(PlatformOperationError) as unknown:
        await transport.send_owner_notice(42, 'follower arrived')
    assert unknown.value.reason == 'unknown_delivery'
    assert len(owner.messages) == 1
