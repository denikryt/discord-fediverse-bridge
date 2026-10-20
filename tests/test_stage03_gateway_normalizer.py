"""Stage 03 Discord Gateway to connector-scoped DTO contracts without SDK I/O."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.platform_types import MessageCreated, MessageDeleted, MessageUpdated, ThreadCreated, ThreadDeleted
from src.discord_gateway_events import DiscordGatewayNormalizer


NOW = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)


def _raw_message(*, channel_id=123, content="hi", author_id=567, bot=False, webhook_id=None, parent=None):
    """Construct only SDK fields actually observed by high-level on_message."""
    author = SimpleNamespace(id=author_id, bot=bot, display_name="Alice")
    channel = SimpleNamespace(id=channel_id, guild=SimpleNamespace(id=77), parent=parent)
    return SimpleNamespace(id=999, channel=channel, guild=channel.guild,
                           author=author, content=content, webhook_id=webhook_id,
                           created_at=NOW, reference=None, type="default")


def test_message_created_preserves_source_timestamp_and_verified_container():
    """An author-origin post keeps its SDK creation time, not local receipt time."""
    normalizer = DiscordGatewayNormalizer("discord-primary", owned_bot_id="42")
    raw = _raw_message()
    event = normalizer.message_created(raw, channel_kind="text")
    assert isinstance(event, MessageCreated)
    assert (event.message.external_container_id, event.message.external_message_id) == ("123", "999")
    assert event.created_at is NOW
    assert event.parent_channel.kind == "text"
    assert event.thread_context is None
    assert event.author.kind == "human"
    assert not event.author.is_bridge_owned


def test_raw_delete_is_id_only_and_never_fetches_or_synthesizes_parent():
    """A cache-less deletion is still representable without author or thread data."""
    normalizer = DiscordGatewayNormalizer("discord-primary", owned_bot_id="42")
    event = normalizer.raw_message_deleted(
        SimpleNamespace(message_id=3, channel_id=20, guild_id=77)
    )
    assert isinstance(event, MessageDeleted)
    assert event.message.external_container_id == "20"
    assert event.parent_channel is None and event.thread_context is None
    assert event.actor is None


def test_raw_edit_absent_body_is_not_empty_body():
    """Missing content is unknown, whereas an explicit empty body is a mutation."""
    normalizer = DiscordGatewayNormalizer("discord-primary", owned_bot_id="42")
    first = normalizer.raw_message_updated(
        SimpleNamespace(message_id=1, channel_id=2, guild_id=3, data={"id":"1"})
    )
    second = normalizer.raw_message_updated(
        SimpleNamespace(message_id=1, channel_id=2, guild_id=3,
                        data={"id":"1", "content": ""})
    )
    assert isinstance(first, MessageUpdated) and first.content is None
    assert second.content.body == ""
    assert first.edited_at is None and first.author is None


def test_trusted_bot_or_webhook_origin_distinct_from_forged_human():
    """Only verified SDK identity, never display text, establishes bridge ownership."""
    normalizer = DiscordGatewayNormalizer("discord-primary", owned_bot_id="42")
    owned = normalizer.message_created(_raw_message(author_id=42, bot=True), channel_kind="text")
    fake = normalizer.message_created(_raw_message(author_id=52), channel_kind="text")
    assert owned.author.kind == "bot" and owned.author.is_bridge_owned
    assert fake.author.kind == "human" and not fake.author.is_bridge_owned
    assert normalizer.message_created(_raw_message(author_id=42), channel_kind="text").author.is_bridge_owned is False


def test_thread_create_only_with_verified_forum_and_never_guesses_starter():
    """No 'newly_created=True' or guessed starter appears without independent proof."""
    normalizer = DiscordGatewayNormalizer("discord-primary", owned_bot_id="42")
    parent = SimpleNamespace(id=9, guild=SimpleNamespace(id=77))
    raw = SimpleNamespace(id=60, parent_id=9, parent=parent, guild=parent.guild,
                          name="topic", owner_id=111, starter_message=None)
    event = normalizer.thread_created(raw, parent_kind="forum")
    assert isinstance(event, ThreadCreated)
    assert event.thread.channel.external_channel_id == "9"
    assert event.starter is None and event.newly_created is None
    # Stage 03 also normalizes verified text-root discussion threads; only
    # the repository can decide whether that physical thread is a Personal Feed root.
    assert normalizer.thread_created(raw, parent_kind="text").thread.channel.kind == "text"
    with pytest.raises(ValueError, match="parent"):
        normalizer.thread_created(SimpleNamespace(**{**raw.__dict__, "parent_id": 3}), parent_kind="forum")


def test_raw_thread_delete_carries_unknown_parent_and_preserves_guild():
    """A cache miss may remove the physical thread without inventing AP ownership."""
    normalizer = DiscordGatewayNormalizer("discord-primary", owned_bot_id="42")
    event = normalizer.raw_thread_deleted(SimpleNamespace(id=60, parent_id=9, guild_id=77))
    assert isinstance(event, ThreadDeleted)
    assert event.thread.channel.kind == "unknown"
    assert event.external_guild_id == "77"
    with pytest.raises(ValueError):
        normalizer.raw_thread_deleted(SimpleNamespace(id=60, parent_id=None, guild_id=77))


def test_author_missing_id_does_not_claim_bridge_ownership():
    """No verified snowflake means no bridge ownership, even when bot flag is true."""
    normalizer = DiscordGatewayNormalizer("discord-primary", owned_bot_id=None)
    raw = _raw_message(author_id=None, bot=True)
    event = normalizer.message_created(raw, channel_kind="text")
    assert event.author.external_actor_id is None
    assert event.author.is_bridge_owned is False


def test_guild_mismatch_is_rejected_at_gateway_conversion():
    """Conflicting cache/SDK guild evidence fails before the domain ingress."""
    normalizer = DiscordGatewayNormalizer("discord-primary", owned_bot_id="42")
    raw = _raw_message()
    raw.channel.guild = SimpleNamespace(id=123)
    with pytest.raises(ValueError, match="guild"):
        normalizer.message_created(raw, channel_kind="text")


def test_thread_message_without_cached_parent_is_not_misclassified_as_root():
    """An unresolved thread parent never turns a forum reply into a feed root."""
    normalizer = DiscordGatewayNormalizer('discord-primary', owned_bot_id='42')
    raw = _raw_message()
    # discord.py may know parent_id without caching the parent object.
    raw.channel.parent_id = 4242
    raw.channel.parent = None
    with pytest.raises(ValueError, match='parent'):
        normalizer.message_created(raw, channel_kind='text')


def test_message_in_verified_thread_retains_exact_immediate_container():
    """An observed parent produces a typed thread with the actual forum ID."""
    normalizer = DiscordGatewayNormalizer('discord-primary', owned_bot_id='42')
    raw = _raw_message(channel_id=100)
    raw.channel.parent = SimpleNamespace(id=4242)
    raw.channel.parent_id = 4242
    event = normalizer.message_created(raw, channel_kind='unknown', parent_kind='forum')
    assert event.message.external_container_id == '100'
    assert event.thread_context.external_thread_id == '100'
    assert event.parent_channel.external_channel_id == '4242'
    assert event.parent_channel.kind == 'forum'
