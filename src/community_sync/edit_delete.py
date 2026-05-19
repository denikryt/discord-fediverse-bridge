"""Edit and delete propagation helpers for shared community sync.

These helpers keep delivery filtering and AP-actor lookup out of the main
runtime while preserving the current edit/delete behavior.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

logger = logging.getLogger(__name__)


def get_outbound_edit_deliveries(all_deliveries: list[object]) -> list[object]:
    """Return bot-owned mirror deliveries that should receive Discord edits."""
    return [delivery for delivery in all_deliveries if delivery.role == "mirror"]


def get_outbound_delete_deliveries(all_deliveries: list[object]) -> list[object]:
    """Return non-source deliveries that should be removed on source delete."""
    return [delivery for delivery in all_deliveries if delivery.role != "source"]


def get_inbound_comment_edit_deliveries(all_deliveries: list[object]) -> list[object]:
    """Return bot-owned inbound/mirror deliveries for inbound comment edits."""
    return [delivery for delivery in all_deliveries if delivery.role in ("inbound", "mirror")]


async def resolve_actor_username(database: object, message_group: object) -> str | None:
    """Resolve the AP actor username for one source message group.

    Update/Delete activities must be authored by the same local actor that
    created the original AP object.
    """
    from ..models import MessageMapping, User

    source_message_id = getattr(message_group, "source_message_id", None)
    if source_message_id is None:
        return None

    with database.session() as session:
        mapping = session.scalar(
            select(MessageMapping).where(
                MessageMapping.discord_message_id == str(source_message_id)
            )
        )
        if mapping is None:
            return None

        user = session.scalar(select(User).where(User.actor_url == mapping.actor_url))
        return user.activitypub_username if user else None


async def propagate_inbound_post_update(
    *,
    bot: object,
    thread_deliveries: list[object],
    new_content: str,
) -> None:
    """Edit all inbound post starter messages concurrently."""

    async def edit_thread_starter(delivery: object) -> None:
        try:
            thread = await bot.get_thread_by_id(delivery.discord_thread_id)
            starter = await thread.fetch_message(delivery.discord_starter_message_id)
            await starter.edit(content=new_content)
            logger.info("Edited inbound post starter in thread %s", delivery.discord_thread_id)
        except Exception:
            logger.exception(
                "Failed to edit inbound post starter in thread %s",
                delivery.discord_thread_id,
            )

    await asyncio.gather(
        *[edit_thread_starter(delivery) for delivery in thread_deliveries],
        return_exceptions=True,
    )


async def propagate_inbound_post_delete(
    *,
    bot: object,
    thread_deliveries: list[object],
) -> None:
    """Mark all inbound post starter messages deleted concurrently."""

    async def mark_starter_deleted(delivery: object) -> None:
        try:
            thread = await bot.get_thread_by_id(delivery.discord_thread_id)
            starter = await thread.fetch_message(delivery.discord_starter_message_id)
            await starter.edit(content="*deleted by creator*")
            logger.info(
                "Marked inbound post starter deleted in thread %s",
                delivery.discord_thread_id,
            )
        except Exception:
            logger.exception(
                "Failed to mark inbound post starter deleted in thread %s",
                delivery.discord_thread_id,
            )

    await asyncio.gather(
        *[mark_starter_deleted(delivery) for delivery in thread_deliveries],
        return_exceptions=True,
    )


async def propagate_inbound_comment_update(
    *,
    bot: object,
    deliveries: list[object],
    new_content: str,
) -> None:
    """Edit all bot-owned inbound comment messages concurrently."""
    from ..formatting import apply_edit_to_discord_message

    async def edit_message(delivery: object) -> None:
        try:
            thread = await bot.get_thread_by_id(delivery.discord_thread_id)
            message = await thread.fetch_message(delivery.discord_message_id)
            updated = apply_edit_to_discord_message(message.content, new_content)
            await message.edit(content=updated)
            logger.info(
                "Edited inbound comment message %s in thread %s",
                delivery.discord_message_id,
                delivery.discord_thread_id,
            )
        except Exception:
            logger.exception(
                "Failed to edit inbound comment message %s in thread %s",
                delivery.discord_message_id,
                delivery.discord_thread_id,
            )

    await asyncio.gather(
        *[edit_message(delivery) for delivery in deliveries],
        return_exceptions=True,
    )


async def propagate_inbound_comment_delete(
    *,
    bot: object,
    deliveries: list[object],
) -> None:
    """Mark all bot-owned inbound comment messages deleted concurrently."""

    async def mark_message_deleted(delivery: object) -> None:
        try:
            thread = await bot.get_thread_by_id(delivery.discord_thread_id)
            message = await thread.fetch_message(delivery.discord_message_id)
            await message.edit(content="*deleted by creator*")
            logger.info(
                "Marked inbound comment message %s deleted in thread %s",
                delivery.discord_message_id,
                delivery.discord_thread_id,
            )
        except Exception:
            logger.exception(
                "Failed to mark inbound comment message %s deleted in thread %s",
                delivery.discord_message_id,
                delivery.discord_thread_id,
            )

    await asyncio.gather(
        *[mark_message_deleted(delivery) for delivery in deliveries],
        return_exceptions=True,
    )
