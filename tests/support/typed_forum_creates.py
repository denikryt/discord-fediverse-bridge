"""Verified typed forum events for router scenarios, without SDK Create fallbacks.

These values model the exact source container, observed author and guild that
DiscordAdapter would authenticate before PlatformEventIngress is invoked.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.platform_types import (
    AuthorRef, ChannelRef, ContentDocument, MessageCreated, MessageRef,
    MessageView, ThreadCreated, ThreadRef,
)

_SOURCE_TIME = datetime(2026, 10, 20, tzinfo=timezone.utc)


def verified_forum_thread(*, forum_id: int, thread_id: int, starter_id: int,
                          guild_id: int = 10) -> tuple[ThreadCreated, MessageView]:
    """Build one observed forum starter with matching physical and author identities."""
    forum = ChannelRef('discord-primary', str(forum_id), 'forum', str(guild_id))
    thread = ThreadRef(forum, str(thread_id))
    starter = MessageRef('discord-primary', str(thread_id), str(starter_id))
    event = ThreadCreated('discord-primary', thread, 'Title',
                          starter=starter, external_guild_id=str(guild_id))
    view = MessageView(starter, AuthorRef('discord-primary', '123', 'human', 'Alice'),
                       ContentDocument('Starter body'), created_at=_SOURCE_TIME)
    return event, view


def verified_forum_reply(*, forum_id: int, thread_id: int, message_id: int,
                         guild_id: int = 10) -> MessageCreated:
    """Build an independently verified human reply scoped to its own thread."""
    forum = ChannelRef('discord-primary', str(forum_id), 'forum', str(guild_id))
    thread = ThreadRef(forum, str(thread_id))
    return MessageCreated(
        'discord-primary', MessageRef('discord-primary', str(thread_id), str(message_id)),
        AuthorRef('discord-primary', '123', 'human', 'Alice'),
        ContentDocument('Reply body'),
        parent_channel=forum, thread_context=thread,
        external_guild_id=str(guild_id), created_at=_SOURCE_TIME,
    )


async def route_verified_forum_thread(router, *, forum_id: int, thread_id: int, starter_id: int):
    """Invoke the real router with one verified starter fetched via the adapter port.

    The stub supplies only an exact physical observation, never a publication
    or a synthetic receipt; the router still enforces current DB ownership.
    """
    from unittest.mock import AsyncMock

    event, observed = verified_forum_thread(
        forum_id=forum_id, thread_id=thread_id, starter_id=starter_id,
    )

    class VerifiedAdapter:
        """Expose only the authenticated, precisely scoped starter read."""

        def __init__(self):
            """Record one deterministic read result without network access."""
            self.fetch_message = AsyncMock(return_value=observed)

    router.bind_reply_notice_adapter(VerifiedAdapter())
    return await router.handle_platform_thread_create(event)
