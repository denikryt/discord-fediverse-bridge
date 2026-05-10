"""Local Discord fanout for mirror thread delivery.

DiscordFanout creates mirror copies of Discord threads and messages in sibling
subscribed channels. It does not touch ActivityPub and does not decide whether
to mirror — CommunityRuntime makes that decision and calls DiscordFanout with
the already-resolved sibling targets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from ..discord_bot import BridgeBot

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MirrorResult:
    """Describe one successfully created mirror thread in a sibling channel."""

    channel_id: int
    thread_id: int
    starter_message_id: int


def _format_mirror_body(message: discord.Message | object) -> str:
    """Build the mirror thread body from the source starter message.

    Format:
        [bridge] {author_display_name}

        {content}

    The author line attributes the original poster so readers know who wrote it,
    even though the bot's account owns the mirror post.
    """
    author = getattr(message, "author", None)
    display_name = getattr(author, "display_name", None) or getattr(author, "name", "unknown")
    content = getattr(message, "content", "") or ""
    return f"[bridge] {display_name}\n\n{content}"


def _unpack_thread_result(result: object) -> tuple[object, object]:
    """Extract (thread, message) from discord.py's create_thread return value.

    discord.py returns either a (thread, message) tuple or a SimpleNamespace-like
    object with .thread and .message attributes. This helper normalises both
    shapes, matching the pattern already used in bridge_lemmy_to_discord.py.
    """
    thread = getattr(result, "thread", None)
    message = getattr(result, "message", None)
    if thread is None and isinstance(result, tuple):
        thread = result[0]
    if message is None and isinstance(result, tuple):
        message = result[1]
    if thread is None:
        raise RuntimeError("create_thread did not return a thread object")
    if message is None:
        raise RuntimeError("create_thread did not return a starter message")
    return thread, message


class DiscordFanout:
    """Deliver local Discord mirror copies of threads and messages.

    DiscordFanout does not touch AP and does not decide whether to mirror.
    CommunityRuntime makes that decision and calls DiscordFanout with the
    already-resolved sibling targets.
    """

    def __init__(self, *, bot: BridgeBot) -> None:
        """Initialise fanout with the shared bot instance for Discord API calls."""
        self.bot = bot

    async def mirror_thread_to_siblings(
        self,
        *,
        source_thread: discord.Thread | object,
        source_starter_message: discord.Message | object,
        sibling_channel_ids: list[int],
    ) -> list[MirrorResult]:
        """Create one mirror thread in each sibling channel.

        Returns one MirrorResult per successfully created mirror thread.
        A sibling channel that fails does not block the others — the error is
        logged and that channel is skipped. This preserves partial-success
        behaviour so source publish is never rolled back for a mirror failure.
        """
        results: list[MirrorResult] = []
        title = getattr(source_thread, "name", "Untitled")
        body = _format_mirror_body(source_starter_message)

        for channel_id in sibling_channel_ids:
            try:
                forum_channel = await self.bot.fetch_forum_channel(channel_id)
                result = await forum_channel.create_thread(name=title, content=body)
                thread, message = _unpack_thread_result(result)
                results.append(MirrorResult(
                    channel_id=channel_id,
                    thread_id=thread.id,
                    starter_message_id=message.id,
                ))
                logger.info(
                    "Mirrored thread %s into channel %s as thread %s",
                    getattr(source_thread, "id", "?"), channel_id, thread.id,
                )
            except Exception:
                logger.exception(
                    "Failed to mirror thread %s into channel %s",
                    getattr(source_thread, "id", "?"), channel_id,
                )

        return results
