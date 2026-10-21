"""Discord-only integer repository key projection from connector-scoped refs."""
from __future__ import annotations

import pytest

from src.platform_types import AuthorRef, ChannelRef, MessageRef, PlatformOperationError, ThreadRef
from src.discord_legacy_projection import discord_id_to_legacy_int


def test_verified_discord_refs_project_only_the_requested_physical_id():
    """An observed message/container, forum/thread, channel and actor stay distinct."""
    channel = ChannelRef('discord-primary', '100', 'forum', '900')
    thread = ThreadRef(channel, '201')
    message = MessageRef('discord-primary', '201', '202')
    actor = AuthorRef('discord-primary', '301', 'human', 'Alice')
    assert discord_id_to_legacy_int(channel) == 100
    assert discord_id_to_legacy_int(thread) == 201
    assert discord_id_to_legacy_int(thread, component='parent') == 100
    assert discord_id_to_legacy_int(message) == 202
    assert discord_id_to_legacy_int(message, component='container') == 201
    assert discord_id_to_legacy_int(actor) == 301


@pytest.mark.parametrize('ref', [
    ChannelRef('fluxer-main', '100', 'forum'),
    ThreadRef(ChannelRef('fluxer-main', '100'), '201'),
    MessageRef('fluxer-main', '201', '202'),
    AuthorRef('fluxer-main', '301', 'human', ''),
])
def test_numeric_fluxer_ids_can_never_collide_with_discord_legacy_keys(ref):
    """All incompatible connector refs fail before any integer repository call."""
    with pytest.raises(PlatformOperationError, match='connector'):
        discord_id_to_legacy_int(ref)


@pytest.mark.parametrize('ref', [
    ChannelRef('discord-primary', '0'),
    MessageRef('discord-primary', '10', 'non-numeric'),
    AuthorRef('discord-primary', None, 'unknown', ''),
])
def test_unverified_or_zero_id_never_becomes_legacy_key(ref):
    """Legacy rows must not be addressed with unknown or malformed snowflakes."""
    with pytest.raises(PlatformOperationError, match='snowflake'):
        discord_id_to_legacy_int(ref)


def test_component_not_supported_by_reference_is_rejected():
    """A channel cannot silently impersonate the immediate message container."""
    with pytest.raises(PlatformOperationError, match='component'):
        discord_id_to_legacy_int(ChannelRef('discord-primary', '100'), component='container')
