"""Inbound parent-post backfill helpers for shared community sync."""

from __future__ import annotations

import logging

from .inbound_mapping import get_accepted_subscriptions

logger = logging.getLogger(__name__)


async def backfill_post_as_thread_group(
    *,
    post_ap_id: str,
    community_actor_id: str,
    delivery_id: str,
    bot: object,
    database: object,
) -> object | None:
    """Fetch a missing AP post and create any missing Discord thread deliveries.

    The helper preserves the current behavior:

    - fetch the AP Page
    - synthesize a `post.created` event
    - create the thread group when missing
    - create only deliveries for channels that still do not have one
    - return `None` on fetch or parse failure so the caller can defer
    """
    from ..bridge_lemmy_to_discord import (
        _build_post_event_from_ap_doc,
        _create_inbound_discord_thread,
        _fetch_ap_object,
    )

    try:
        document = await _fetch_ap_object(post_ap_id)
    except Exception:
        logger.exception("Backfill fetch failed for post %s", post_ap_id)
        return None

    try:
        post_event = _build_post_event_from_ap_doc(document, community_actor_id, delivery_id)
    except Exception:
        logger.exception("Backfill parse failed for post %s", post_ap_id)
        return None

    thread_group = database.discord_fanout_groups.get_thread_group_by_ap_object(post_ap_id)
    if thread_group is None:
        thread_group = database.discord_fanout_groups.create_thread_group(
            community_actor_id=community_actor_id,
            source_channel_id=None,
            source_thread_id=None,
            source_starter_message_id=None,
            ap_activity_id=delivery_id,
            ap_object_id=post_ap_id,
        )

    already_delivered_channels = {
        delivery.discord_channel_id for delivery in database.discord_fanout_groups.get_thread_deliveries(thread_group.id)
    }
    accepted = get_accepted_subscriptions(database, community_actor_id)

    await bot.wait_until_bridge_ready()

    for subscription in accepted:
        if subscription.discord_channel_id in already_delivered_channels:
            continue
        try:
            forum_channel = await bot.fetch_forum_channel(subscription.discord_channel_id)
            thread_id, starter_message_id = await _create_inbound_discord_thread(
                forum_channel=forum_channel,
                event=post_event,
            )
            database.discord_fanout_groups.add_thread_delivery(
                thread_group_id=thread_group.id,
                discord_channel_id=subscription.discord_channel_id,
                discord_thread_id=thread_id,
                discord_starter_message_id=starter_message_id,
                role="inbound",
            )
        except Exception:
            logger.exception(
                "Backfill thread creation failed for channel %s post %s",
                subscription.discord_channel_id,
                post_ap_id,
            )

    logger.info(
        "Backfilled post %s into thread group %s (%d subscription(s) processed)",
        post_ap_id,
        thread_group.id,
        len(accepted),
    )
    return thread_group

