"""Tests for Discord forum placement helper behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.discord_forum_placement import (
    ForumPlacementError,
    cleanup_created_forum_channel,
    is_forum_channel_available,
    normalize_forum_channel_name,
    resolve_optional_forum_channel,
)


def _database() -> Mock:
    """Build a repository container fake with free-channel defaults."""
    database = Mock()
    database.local_communities.get_local_community_by_forum_channel_id.return_value = None
    database.remote_subscriptions.get_subscription_by_channel.return_value = None
    database.local_subscribers.get_local_subscriber_by_channel.return_value = None
    return database


def test_normalize_forum_channel_name_conservatively_sanitizes_input() -> None:
    """Auto-created channel names should be predictable and Discord-safe."""
    assert normalize_forum_channel_name("Technology") == "technology"
    assert normalize_forum_channel_name("Tech News") == "tech-news"
    assert normalize_forum_channel_name("technology!!!") == "technology"
    assert normalize_forum_channel_name("  Привет  ") == "community"


def test_channel_availability_checks_all_bridge_binding_tables() -> None:
    """A channel is free only when no bridge channel-binding table owns it."""
    database = _database()
    assert is_forum_channel_available(database, 12345)

    database.remote_subscriptions.get_subscription_by_channel.return_value = SimpleNamespace(status="failed")
    assert not is_forum_channel_available(database, 12345)


@pytest.mark.asyncio
async def test_selected_channel_rejects_when_already_bound() -> None:
    """Selected channels are rejected before command operations can mutate state."""
    database = _database()
    database.local_subscribers.get_local_subscriber_by_channel.return_value = SimpleNamespace(id=1)
    guild = SimpleNamespace(id=99999)
    channel = SimpleNamespace(id=12345, mention="<#12345>")

    with pytest.raises(ForumPlacementError) as exc_info:
        await resolve_optional_forum_channel(
            database=database,
            guild=guild,
            selected_channel=channel,
            desired_name="technology",
            command_name="subscribe-community",
        )

    assert exc_info.value.reason == "channel_unavailable"


@pytest.mark.asyncio
async def test_auto_create_generates_unique_forum_name() -> None:
    """Auto-create appends bounded suffixes when the guild already has a name."""
    database = _database()
    created_channel = SimpleNamespace(id=777, name="technology-3", mention="<#777>", delete=AsyncMock())
    guild = SimpleNamespace(
        id=99999,
        channels=[SimpleNamespace(name="technology"), SimpleNamespace(name="technology-2")],
        me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_channels=True)),
        create_forum=AsyncMock(return_value=created_channel),
    )

    placement = await resolve_optional_forum_channel(
        database=database,
        guild=guild,
        selected_channel=None,
        desired_name="Technology",
        command_name="subscribe-community",
    )

    assert placement.channel is created_channel
    assert placement.created_by_bot is True
    guild.create_forum.assert_awaited_once_with(
        name="technology-3",
        reason="discord-fediverse-bridge subscribe-community auto-create",
    )


@pytest.mark.asyncio
async def test_missing_manage_channels_preflight_rejects_before_create() -> None:
    """Permission preflight avoids a Discord API call when cache says it cannot work."""
    database = _database()
    guild = SimpleNamespace(
        id=99999,
        channels=[],
        me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_channels=False)),
        create_forum=AsyncMock(),
    )

    with pytest.raises(ForumPlacementError) as exc_info:
        await resolve_optional_forum_channel(
            database=database,
            guild=guild,
            selected_channel=None,
            desired_name="technology",
            command_name="create_community",
        )

    assert exc_info.value.reason == "missing_manage_channels"
    guild.create_forum.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_skips_when_persisted_row_owns_created_channel() -> None:
    """Cleanup must not delete a channel that later became owned by a bridge row."""
    database = _database()
    database.remote_subscriptions.get_subscription_by_channel.return_value = SimpleNamespace(status="failed")
    channel = SimpleNamespace(id=12345, delete=AsyncMock())
    logger = Mock()

    await cleanup_created_forum_channel(
        SimpleNamespace(channel=channel, created_by_bot=True),
        database=database,
        logger=logger,
        guild_id=99999,
        command_name="subscribe-community",
        original_reason="follow_dispatch_failed",
    )

    channel.delete.assert_not_awaited()
    logger.warning.assert_called_once()
