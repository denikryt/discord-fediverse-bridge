"""Stage 02 physical-identity, ingress, and outbound admission contracts."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from src.platform_types import (
    AttachmentRef,
    AuthorRef,
    ChannelRef,
    ConnectorRef,
    ContentDocument,
    CreateAttempt,
    CreatedMessage,
    CreatedThread,
    DeliveryPresentation,
    MessageCreated,
    MessageDeleted,
    MessageRef,
    MessageUpdated,
    MessageView,
    PlatformOperationError,
    ThreadCreated,
    ThreadDeleted,
    ThreadRef,
    ThreadUpdated,
    ThreadView,
    validate_create_attempt,
    validate_created_receipt,
    validate_message_reply,
)


def test_connector_metadata_matches_stage01_identity_and_rejects_invalid_origin():
    """Only valid persisted connector snapshots may cross the adapter boundary."""
    assert ConnectorRef('discord-primary', 'discord').instance_origin is None
    assert ConnectorRef('fluxer-a', 'fluxer', 'https://fluxer.test').instance_origin == 'https://fluxer.test'
    for args in [('', 'discord', None), ('discord-primary', 'discord', 'https://wrong.test'),
                 ('fluxer-a', 'fluxer', None), ('fluxer-a', 'fluxer', 'https://x.test/path'),
                 ('key', 'other', None)]:
        with pytest.raises(ValueError):
            ConnectorRef(*args)


def test_channel_and_thread_hash_are_stable_after_metadata_enrichment():
    """A discovered guild/kind does not create a second logical channel or thread."""
    preliminary = ChannelRef('discord-primary', '900')
    verified = ChannelRef('discord-primary', '900', 'forum', '1')
    assert preliminary == verified and hash(preliminary) == hash(verified)
    assert len({preliminary, verified}) == 1
    assert ThreadRef(preliminary, '111') == ThreadRef(verified, '111')
    assert hash(ThreadRef(preliminary, '111')) == hash(ThreadRef(verified, '111'))
    assert ChannelRef('fluxer-a', '900') != verified
    with pytest.raises(ValueError):
        ChannelRef('discord-primary', '900', 'not_forum')
    with pytest.raises(FrozenInstanceError):
        preliminary.kind = 'forum'


def test_message_identity_is_usable_from_raw_delete_without_parent_lookup():
    """Gateway delete carrying immediate thread ID is a complete message identity."""
    message = MessageRef('discord-primary', '111', '333')
    deleted = MessageDeleted(connector_key='discord-primary', message=message)
    assert deleted.message == message
    assert message != MessageRef('discord-primary', '900', '333')
    assert message != MessageRef('fluxer-a', '111', '333')
    with pytest.raises(ValueError):
        MessageDeleted(connector_key='fluxer-a', message=message)
    with pytest.raises(ValueError):
        MessageRef('', '111', '333')


def test_thread_starter_is_distinct_and_verified_when_known():
    """Never infer the starter ID from the forum thread ID."""
    thread = ThreadRef(ChannelRef('discord-primary', '900', 'forum'), '111')
    starter = MessageRef('discord-primary', '111', '222')
    created = CreatedThread(thread, starter, 'bot')
    assert created.starter.external_message_id != created.thread.external_thread_id
    assert ThreadView(thread, starter=starter).starter == starter
    assert ThreadView(thread).starter is None
    with pytest.raises(ValueError):
        CreatedThread(thread, MessageRef('discord-primary', '900', '222'), 'bot')
    with pytest.raises(ValueError):
        ThreadCreated(connector_key='discord-primary', thread=thread, name='name', starter=MessageRef('fluxer-a', '111', '222'))


def test_partial_updates_are_not_empty_content_and_events_are_frozen():
    """Absent edit fields must not inadvertently erase the canonical message."""
    message = MessageRef('discord-primary', '111', '333')
    absent = MessageUpdated(connector_key='discord-primary', message=message)
    explicit = MessageUpdated(connector_key='discord-primary', message=message, content=ContentDocument(''))
    assert absent.content is None
    assert explicit.content.body == ''
    assert absent.edited_at is None
    with pytest.raises(FrozenInstanceError):
        absent.content = explicit.content
    with pytest.raises(ValueError):
        MessageUpdated(connector_key='discord-primary', message=message, thread_context=ThreadRef(ChannelRef('discord-primary', '900'), '444'))


def test_all_six_inbound_event_variants_preserve_connector_scopes():
    """Create/update/delete event families remain typed and independently representable."""
    parent = ChannelRef('discord-primary', '900', 'forum')
    thread = ThreadRef(parent, '111')
    message = MessageRef('discord-primary', '111', '333')
    author = AuthorRef('discord-primary', '44', 'human', 'Alice')
    events = [
        ThreadCreated('discord-primary', thread, 'discussion', owner=author, newly_created=False),
        ThreadUpdated('discord-primary', thread, name='updated'),
        ThreadDeleted('discord-primary', thread),
        MessageCreated('discord-primary', message, author, ContentDocument('hello'), is_starter=None, thread_context=thread),
        MessageUpdated('discord-primary', message, ContentDocument('updated'), edited_at=datetime.now(timezone.utc)),
        MessageDeleted('discord-primary', message),
    ]
    assert len({type(event) for event in events}) == 6
    assert all(event.connector_key == 'discord-primary' for event in events)
    with pytest.raises(ValueError):
        MessageCreated('fluxer-a', message, author, ContentDocument('hello'))
    with pytest.raises(ValueError):
        ThreadUpdated('fluxer-a', thread)


def test_ownership_labels_cannot_turn_human_into_owned_bot():
    """Author observation is never a claim of ActivityPub user ownership."""
    assert AuthorRef('discord-primary', None, 'unknown', '').kind == 'unknown'
    assert AuthorRef('discord-primary', 'bot-id', 'bot', 'mirror', True).is_bridge_owned
    with pytest.raises(ValueError):
        AuthorRef('discord-primary', '1', 'human', 'Alice', True)
    with pytest.raises(ValueError):
        AuthorRef('discord-primary', None, 'human', 'Alice')
    with pytest.raises(ValueError):
        AuthorRef('discord-primary', 'x', 'malicious', 'Alice')


def test_neutral_content_and_presentation_do_not_require_sdk_objects():
    """Attachments are descriptors; webhook mode is explicit, not author ownership."""
    document = ContentDocument('hello <@123>', attachments=(AttachmentRef('https://example.test/a', 'a.png'),))
    assert document.attachments[0].url.startswith('https://')
    assert DeliveryPresentation('webhook', display_name='Remote user').mode == 'webhook'
    with pytest.raises(ValueError):
        DeliveryPresentation('random')
    with pytest.raises(ValueError):
        AttachmentRef('file:///secret', 'a.txt')


def test_create_attempt_scope_reply_scope_and_receipt_are_checked_at_boundary():
    """No network create may escape with wrong attempt, reply, or returned destination."""
    forum = ChannelRef('discord-primary', '900', 'forum')
    thread = ThreadRef(forum, '111')
    attempt = CreateAttempt('K', 'discord-primary', 'message', thread)
    validate_create_attempt(attempt, 'message', thread)
    validate_message_reply(thread, MessageRef('discord-primary', '111', 'parent'))
    receipt = CreatedMessage(MessageRef('discord-primary', '111', '333'), 'bot')
    validate_created_receipt(attempt, receipt)
    with pytest.raises(PlatformOperationError) as error:
        validate_create_attempt(attempt, 'message', ChannelRef('discord-primary', '111'))
    assert error.value.reason == 'invalid_request'
    for bad in [MessageRef('discord-primary', '222', 'parent'), MessageRef('fluxer-a', '111', 'parent')]:
        with pytest.raises(PlatformOperationError):
            validate_message_reply(thread, bad)
    with pytest.raises(PlatformOperationError):
        validate_created_receipt(attempt, CreatedMessage(MessageRef('fluxer-a', '111', '333'), 'bot'))
    with pytest.raises(PlatformOperationError):
        validate_created_receipt(attempt, CreatedMessage(MessageRef('discord-primary', '900', '333'), 'bot'))
    with pytest.raises(ValueError):
        CreateAttempt('K', 'discord-primary', 'forum_thread', thread)



@pytest.mark.parametrize('observed', [
    ChannelRef('discord-primary', '900', 'text', '1'),
    ChannelRef('discord-primary', '900', 'forum', '2'),
])
def test_forum_receipt_rejects_conflicting_known_channel_metadata(observed):
    """A physical channel match must not conceal a conflicting kind or guild."""
    forum = ChannelRef('discord-primary', '900', 'forum', '1')
    attempt = CreateAttempt('post', 'discord-primary', 'forum_thread', forum)
    receipt = CreatedThread(ThreadRef(observed, '111'), MessageRef('discord-primary', '111', '222'), 'bot')
    # ChannelRef identity intentionally ignores metadata; receipt admission must not.
    assert receipt.thread.channel == forum
    with pytest.raises(PlatformOperationError) as failure:
        validate_created_receipt(attempt, receipt)
    assert failure.value.reason == 'invalid_request'


@pytest.mark.parametrize('requested,observed', [
    (ChannelRef('discord-primary', '900'), ChannelRef('discord-primary', '900', 'forum', '1')),
    (ChannelRef('discord-primary', '900', 'forum'), ChannelRef('discord-primary', '900', 'forum', '1')),
    (ChannelRef('discord-primary', '900', 'forum', '1'), ChannelRef('discord-primary', '900', 'forum', '1')),
])
def test_forum_receipt_accepts_consistent_metadata_enrichment(requested, observed):
    """Missing metadata may be enriched without changing physical identity."""
    attempt = CreateAttempt('post', 'discord-primary', 'forum_thread', requested)
    receipt = CreatedThread(ThreadRef(observed, '111'), MessageRef('discord-primary', '111', '222'), 'bot')
    validate_created_receipt(attempt, receipt)


def test_unknown_delivery_is_one_unambiguous_reason():
    """An ambiguous create cannot be treated as a definite transport failure."""
    error = PlatformOperationError('unknown_delivery')
    assert error.reason == 'unknown_delivery'
    assert not hasattr(error, 'delivery_state')
    for reason in ['not_found', 'forbidden', 'unsupported', 'invalid_request', 'rate_limited', 'unavailable']:
        assert PlatformOperationError(reason).reason == reason
    with pytest.raises(ValueError):
        PlatformOperationError('perhaps')


def test_opaque_identity_preserves_whitespace_that_stage01_can_store_but_rejects_control_bytes():
    """Metadata must not silently rewrite a persisted opaque connector/channel key."""
    preserved = ChannelRef(' primary ', ' 900 ')
    assert (preserved.connector_key, preserved.external_channel_id) == (' primary ', ' 900 ')
    for invalid in ('', '\x00', '\n', '\t', '   ', 'key\rnext'):
        with pytest.raises(ValueError):
            MessageRef('discord-primary', invalid, '333')
    with pytest.raises(ValueError):
        AuthorRef('discord-primary', None, 'webhook', 'mirror', True)


def test_resolved_guild_claims_reject_conflicting_event_metadata_without_requiring_it():
    """Known contradictory guild evidence fails at ingress; raw guildless delete works."""
    forum = ChannelRef('discord-primary', '900', 'forum', '1')
    thread = ThreadRef(forum, '111')
    message = MessageRef('discord-primary', '111', '333')
    assert ThreadCreated('discord-primary', thread, 'post').external_guild_id is None
    assert MessageDeleted('discord-primary', message).external_guild_id is None
    with pytest.raises(ValueError):
        ThreadCreated('discord-primary', thread, 'post', external_guild_id='2')
    with pytest.raises(ValueError):
        ThreadUpdated('discord-primary', thread, external_guild_id='2')
    with pytest.raises(ValueError):
        MessageDeleted('discord-primary', message, thread_context=thread, external_guild_id='2')
    with pytest.raises(ValueError):
        MessageCreated('discord-primary', message, AuthorRef('discord-primary', '1', 'human', 'Alice'),
                       ContentDocument('post'), parent_channel=forum, thread_context=thread, external_guild_id='2')


def test_presented_delivery_mode_must_match_created_receipt_at_adapter_boundary():
    """A requested webhook send cannot be silently acknowledged as a bot send."""
    forum = ChannelRef('discord-primary', '900', 'forum')
    attempt = CreateAttempt('send', 'discord-primary', 'message', forum)
    bot_receipt = CreatedMessage(MessageRef('discord-primary', '900', '333'), 'bot')
    validate_created_receipt(attempt, bot_receipt, presentation=DeliveryPresentation())
    with pytest.raises(PlatformOperationError):
        validate_created_receipt(attempt, bot_receipt,
                                 presentation=DeliveryPresentation('webhook'))
    with pytest.raises(PlatformOperationError):
        validate_created_receipt(attempt, ThreadRef(forum, '111'), presentation=DeliveryPresentation())


def test_content_descriptor_requires_urls_not_native_upload_objects_or_credentials():
    """Adapter receives only safe-to-pass link metadata and valid typed file sizes."""
    assert ContentDocument('body', source_url='https://example.test/p').source_url == 'https://example.test/p'
    for url in ('http:unsafe', 'file:///tmp/private', 'https://user:pass@example.test/image'):
        with pytest.raises(ValueError):
            ContentDocument('body', source_url=url)
        with pytest.raises(ValueError):
            DeliveryPresentation(avatar_url=url)
    for size in (-1, True, '120'):
        with pytest.raises(ValueError):
            AttachmentRef('https://example.test/file', 'file.bin', size=size)
    with pytest.raises(ValueError):
        ContentDocument('x', attachments=['an SDK File'])


def test_stage02_action_surface_does_not_grow_unused_physical_thread_methods():
    """Inbound physical mutations need not imply unused outbound thread controls."""
    from typing import get_args

    from src.platform_types import PlatformAction
    from src.runtime_ports import PlatformDeliveryPort

    assert 'edit_message' in get_args(PlatformAction)
    assert 'delete_message' in get_args(PlatformAction)
    assert 'manage_channels' not in get_args(PlatformAction)
    assert not hasattr(PlatformDeliveryPort, 'edit_thread')
    assert not hasattr(PlatformDeliveryPort, 'delete_thread')

@pytest.mark.parametrize('event_type', [MessageCreated, MessageUpdated, MessageDeleted])
def test_inbound_message_event_rejects_conflicting_known_channel_metadata(event_type):
    """Gateway enrichment may not assign two known guilds/kinds to one channel."""
    author = AuthorRef('discord-primary', 'user-1', 'human', 'Reader')
    message = MessageRef('discord-primary', '111', '333')
    forum = ChannelRef('discord-primary', '900', 'forum', '1')
    event_data = {'connector_key': 'discord-primary', 'message': message}
    if event_type is MessageCreated:
        event_data.update(author=author, content=ContentDocument('posted'))

    # A partially discovered parent is valid; a later known identity must not
    # contradict the original's known metadata.
    event = event_type(**event_data, parent_channel=forum,
                       thread_context=ThreadRef(ChannelRef('discord-primary', '900', 'unknown'), '111'))
    assert event.parent_channel == event.thread_context.channel
    for other in (ChannelRef('discord-primary', '900', 'forum', '2'),
                  ChannelRef('discord-primary', '900', 'text', '1')):
        with pytest.raises(ValueError):
            event_type(**event_data, parent_channel=forum,
                       thread_context=ThreadRef(other, '111'))


@pytest.mark.parametrize('event_type', [MessageCreated, MessageUpdated, MessageDeleted])
def test_inbound_event_guild_verifies_every_available_context(event_type):
    """A known guild in either context cannot disagree with an event guild."""
    author = AuthorRef('discord-primary', 'user-1', 'human', 'Reader')
    message = MessageRef('discord-primary', '111', '333')
    event_data = {'connector_key': 'discord-primary', 'message': message}
    if event_type is MessageCreated:
        event_data.update(author=author, content=ContentDocument('posted'))
    unknown_parent = ChannelRef('discord-primary', '900', 'unknown')
    observed_thread = ThreadRef(ChannelRef('discord-primary', '900', 'forum', '2'), '111')
    # Conflicting event ID must not be hidden by an earlier parent with no guild.
    with pytest.raises(ValueError):
        event_type(**event_data, parent_channel=unknown_parent,
                   thread_context=observed_thread, external_guild_id='1')
    assert event_type(**event_data, parent_channel=unknown_parent,
                      thread_context=observed_thread, external_guild_id='2').external_guild_id == '2'


def test_inbound_thread_starter_message_can_reference_parent_container():
    """THREAD_STARTER_MESSAGE can refer to a root message in its parent channel."""
    forum = ChannelRef('discord-primary', '900', 'forum', '1')
    thread = ThreadRef(forum, '111')
    event = MessageCreated(
        connector_key='discord-primary',
        message=MessageRef('discord-primary', '111', '333'),
        author=AuthorRef('discord-primary', 'bot', 'bot', 'bridge'),
        content=ContentDocument('starter'),
        reply_to=MessageRef('discord-primary', '900', '111'),
        parent_channel=forum,
        thread_context=thread,
    )
    assert event.reply_to.external_container_id == '900'


@pytest.mark.parametrize('bad', ['yesterday', 123, datetime(2026, 10, 17), object()])
def test_message_edit_timestamp_rejects_invalid_or_naive_values(bad):
    """An inbound edit cannot introduce incomparable timestamp representations."""
    message = MessageRef('discord-primary', '111', '333')
    with pytest.raises(ValueError):
        MessageUpdated('discord-primary', message, edited_at=bad)
    with pytest.raises(ValueError):
        MessageView(message, edited_at=bad)


def test_message_edit_timestamp_accepts_aware_datetime_and_absence():
    """UTC and offset-aware platform edits remain comparable; None means unknown."""
    message = MessageRef('discord-primary', '111', '333')
    aware = datetime(2026, 10, 17, 13, 30, tzinfo=timezone.utc)
    assert MessageUpdated('discord-primary', message, edited_at=aware).edited_at == aware
    assert MessageView(message, edited_at=aware).edited_at == aware
    assert MessageUpdated('discord-primary', message).edited_at is None


@pytest.mark.parametrize('event_type', [ThreadCreated, ThreadUpdated, ThreadDeleted,
                                       MessageCreated, MessageUpdated, MessageDeleted])
def test_inbound_received_timestamp_requires_timezone_aware_datetime(event_type):
    """All SDK-normalized event times use comparable aware datetime instants."""
    thread = ThreadRef(ChannelRef('discord-primary', '900', 'forum', '1'), '111')
    message = MessageRef('discord-primary', '111', '333')
    payload = {'connector_key': 'discord-primary', 'thread': thread} if event_type in (
        ThreadCreated, ThreadUpdated, ThreadDeleted) else {
            'connector_key': 'discord-primary', 'message': message,
        }
    if event_type is ThreadCreated:
        payload['name'] = 'discussion'
    if event_type is MessageCreated:
        payload.update(author=AuthorRef('discord-primary', 'user', 'human', 'Reader'),
                       content=ContentDocument('hello'))
    for invalid in ('yesterday', datetime(2026, 10, 17), None):
        with pytest.raises(ValueError):
            event_type(**payload, received_at=invalid)
    aware = datetime(2026, 10, 17, tzinfo=timezone.utc)
    assert event_type(**payload, received_at=aware).received_at == aware


def test_created_message_preserves_source_creation_instant_not_received_instant():
    """A valid Discord source timestamp survives conversion without losing its offset."""
    from datetime import timedelta

    message = MessageRef('discord-primary', 'thread-1', 'message-1')
    author = AuthorRef('discord-primary', 'user-1', 'human', 'Example')
    published = datetime(2026, 10, 18, 21, 30, tzinfo=timezone(timedelta(hours=3)))
    observed = datetime(2026, 10, 18, 22, 00, tzinfo=timezone.utc)
    event = MessageCreated('discord-primary', message, author, ContentDocument('body'),
                           created_at=published, received_at=observed)
    assert event.created_at == published
    assert event.received_at == observed
    assert event.created_at != event.received_at
    assert MessageCreated('discord-primary', message, author, ContentDocument('body')).created_at is None


def test_created_message_rejects_unverified_or_naive_source_timestamp():
    """A missing timestamp is allowed but a supplied naive timestamp is unsafe."""
    message = MessageRef('discord-primary', 'thread-1', 'message-1')
    author = AuthorRef('discord-primary', 'user-1', 'human', 'Example')
    for invalid in (datetime(2026, 10, 18), '2026-10-18T00:00:00Z'):
        with pytest.raises(ValueError, match='created_at'):
            MessageCreated('discord-primary', message, author, ContentDocument('body'),
                           created_at=invalid)


def test_partial_edit_author_is_optional_but_must_match_connector():
    """Best-effort edit attribution cannot introduce a cross-platform author."""
    message = MessageRef('discord-primary', 'thread-1', 'message-1')
    valid = AuthorRef('discord-primary', 'user-1', 'human', 'Example')
    assert MessageUpdated('discord-primary', message).author is None
    assert MessageUpdated('discord-primary', message, author=valid).author == valid
    with pytest.raises(ValueError, match='author'):
        MessageUpdated('discord-primary', message,
                       author=AuthorRef('fluxer-a', 'user-1', 'human', 'Example'))
