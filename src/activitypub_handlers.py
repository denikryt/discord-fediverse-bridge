from __future__ import annotations

import logging
from dataclasses import dataclass

from .activitypub_models import ActivityPubEvent
from .bridge_lemmy_to_discord import create_discord_message_for_activitypub_comment, create_discord_thread_for_activitypub_post
from .runtime import Runtime

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class HandlerResult:
    # Handlers return explicit processing status so HTTP receipts can record
    # whether the event was processed, skipped, or retried later.
    status: str
    detail: str


async def dispatch_activitypub_event(event: ActivityPubEvent, runtime: Runtime) -> HandlerResult:
    # Keep dispatch explicit so supported inbound event types stay obvious.
    if event.event_type == "post.created":
        return await handle_post_created(event, runtime)
    if event.event_type == "comment.created":
        return await handle_comment_created(event, runtime)
    raise RuntimeError(f"Unsupported event type: {event.event_type}")


async def handle_post_created(event: ActivityPubEvent, runtime: Runtime) -> HandlerResult:
    # The bridge is intentionally single-community for now, so foreign events
    # are skipped before any Discord lookup or write.
    if event.community_actor_id != runtime.settings.lemmy_community_actor_id:
        return HandlerResult(status="skipped", detail="community actor mismatch")

    await runtime.bot.wait_until_bridge_ready()
    forum_channel = runtime.bot.require_forum_channel()
    if runtime.database.get_post_link_by_lemmy_post_ap_id(event.object.ap_id) is not None:
        return HandlerResult(status="skipped", detail="post already linked")

    await create_discord_thread_for_activitypub_post(
        database=runtime.database,
        forum_channel=forum_channel,
        event=event,
    )
    return HandlerResult(status="processed", detail="post created")


async def handle_comment_created(event: ActivityPubEvent, runtime: Runtime) -> HandlerResult:
    if event.community_actor_id != runtime.settings.lemmy_community_actor_id:
        return HandlerResult(status="skipped", detail="community actor mismatch")

    await runtime.bot.wait_until_bridge_ready()
    if runtime.database.get_comment_link_by_lemmy_comment_ap_id(event.object.ap_id) is not None:
        return HandlerResult(status="skipped", detail="comment already linked")

    post_link = runtime.database.get_post_link_by_lemmy_post_ap_id(event.object.post_ap_id or "")
    if post_link is None:
        # Comment delivery is only safe after the parent post has already been
        # mapped to a Discord thread.
        logger.info("Skipping ActivityPub comment %s because post %s is not mapped yet", event.object.ap_id, event.object.post_ap_id)
        return HandlerResult(status="skipped", detail="parent post is not mapped")

    resolved_thread = await runtime.bot.get_thread_by_id(post_link.discord_forum_thread_id)
    await create_discord_message_for_activitypub_comment(
        database=runtime.database,
        thread=resolved_thread,
        event=event,
    )
    return HandlerResult(status="processed", detail="comment created")
