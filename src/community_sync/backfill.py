"""Inbound parent-post backfill helpers for shared community sync."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from ..db import Database
    from ..runtime_ports import PlatformDeliveryPort
    from .runtime import CommunityRuntime

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

    accepted = get_accepted_subscriptions(database, community_actor_id)
    if not accepted:
        # Preserve any historical canonical context without creating a new
        # Discord group or fetching AP content solely for a non-Discord owner.
        return database.discord_fanout_groups.get_thread_group_by_ap_object(post_ap_id)

    try:
        document = await _fetch_ap_object(post_ap_id)
    except Exception:
        logger.exception("Backfill fetch failed for post %s", post_ap_id)
        return None

    try:
        post_event = _build_post_event_from_ap_doc(
            document, community_actor_id, delivery_id
        )
    except Exception:
        logger.exception("Backfill parse failed for post %s", post_ap_id)
        return None

    thread_group = database.discord_fanout_groups.get_thread_group_by_ap_object(
        post_ap_id
    )
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
        delivery.discord_channel_id
        for delivery in database.discord_fanout_groups.get_thread_deliveries(
            thread_group.id
        )
    }
    await bot.wait_until_bridge_ready()

    for subscription in accepted:
        if subscription.discord_channel_id in already_delivered_channels:
            continue
        try:
            forum_channel = await bot.fetch_forum_channel(
                subscription.discord_channel_id
            )
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


async def backfill_normalized_post_as_thread_group(
    *,
    post_ap_id: str,
    community_actor_id: str,
    delivery_id: str,
    database: Database,
    community_runtime: CommunityRuntime,
    adapter: PlatformDeliveryPort,
    fetch_ap_object: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
) -> object | None:
    """Recover missing AP parent-post forum mappings through the typed transport.

    Remote fetch is used only when a canonical parent group is fully absent.
    Incomplete existing groups are deferred: no stored negative physical receipt
    proves that an ID-less target was not created before a lost HTTP response.
    The fetched document must identify the exact requested AP object.
    """
    from ..bridge_lemmy_to_discord import _build_post_event_from_ap_doc, _fetch_ap_object
    from .inbound_mapping import needs_backfill

    existing = database.discord_fanout_groups.get_thread_group_by_ap_object(post_ap_id)
    if existing is not None and existing.community_actor_id != community_actor_id:
        # A post already bound to another community must never be recovered
        # into this community using a new comment's asserted community ID.
        return None
    if existing is not None and needs_backfill(
        thread_group=existing, community_actor_id=community_actor_id, database=database,
    ):
        # The current schema stores only confirmed target receipts. A missing
        # row may represent a committed Discord Create with a lost HTTP reply;
        # fetching the AP source cannot disprove that physical side effect.
        # Defer dependent comments until local subscriber publishing verifies the gap. No fetch
        # or second create is safe here, even after a process restart.
        logger.warning('Parent post %s has unresolved Discord target receipts', post_ap_id)
        return None
    if not needs_backfill(
        thread_group=existing, community_actor_id=community_actor_id, database=database,
    ):
        return existing

    fetch = fetch_ap_object if fetch_ap_object is not None else _fetch_ap_object
    try:
        document = await fetch(post_ap_id)
        if (not isinstance(document, dict) or document.get('id') != post_ap_id
            or document.get('type') not in ('Page', 'Article')):
            # Do not trust the retrieved URL as proof of the document identity.
            logger.warning('Backfill post identity mismatch for %s', post_ap_id)
            return None
        post_event = _build_post_event_from_ap_doc(
            document, community_actor_id, delivery_id,
        )
    except Exception:
        logger.exception('Normalized backfill retrieval/parse failed for %s', post_ap_id)
        return None

    await community_runtime.handle_normalized_inbound_post(
        post_event, adapter=adapter, recover_missing=True,
    )
    group = database.discord_fanout_groups.get_thread_group_by_ap_object(post_ap_id)
    if group is None or needs_backfill(
        thread_group=group, community_actor_id=community_actor_id, database=database,
    ):
        # A missing or unsuccessful recipient is not a confirmed parent-post
        # delivery. Do not start dependent comment sends based on guesswork.
        return None
    return group
