"""Shared content-level edit and delete helpers.

These helpers operate on generic `published_activity_objects` and concrete
Discord thread/message ids. They deliberately avoid owning any routing policy
or mode-specific row traversal.
"""

from __future__ import annotations

from ..formatting import apply_edit_to_discord_message


def resolve_published_object_for_discord_message(
    database: object, *, discord_message_id: int
) -> object | None:
    """Load the published AP object that originated from one Discord message.

    Both bridge modes persist `published_activity_objects`, so update/delete
    propagation can recover AP ownership without peeking into mode-specific
    tables first.
    """
    return database.activitypub_objects.get_published_activity_object_by_discord_message_id(discord_message_id)


async def edit_discord_message(
    *,
    bot: object,
    discord_thread_id: int,
    discord_message_id: int,
    new_content: str,
    preserve_header: bool,
    fallback_header: str = "",
) -> None:
    """Edit one Discord message, optionally preserving the existing header line."""
    thread = await bot.get_thread_by_id(discord_thread_id)
    message = await thread.fetch_message(discord_message_id)
    if preserve_header:
        content = apply_edit_to_discord_message(
            message.content,
            new_content,
            fallback_header=fallback_header,
        )
    else:
        content = new_content
    await message.edit(content=content)


async def mark_discord_message_deleted(
    *,
    bot: object,
    discord_thread_id: int,
    discord_message_id: int,
) -> None:
    """Mark one mirrored Discord message as deleted without removing it."""
    thread = await bot.get_thread_by_id(discord_thread_id)
    message = await thread.fetch_message(discord_message_id)
    await message.edit(content="*deleted by creator*")
