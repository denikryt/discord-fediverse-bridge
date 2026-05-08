from __future__ import annotations

import logging
from dataclasses import dataclass

from .activitypub_models import (
    ActivityPubEvent,
    BridgeGatewayEvent,
    FollowLifecycleEvent,
)
from .bridge_lemmy_to_discord import create_discord_message_for_activitypub_comment, create_discord_thread_for_activitypub_post
from .runtime import Runtime

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class HandlerResult:
    # Handlers return explicit processing status so HTTP receipts can record
    # whether the event was processed, skipped, or retried later.
    status: str
    detail: str


async def dispatch_activitypub_event(
    event: BridgeGatewayEvent, runtime: Runtime
) -> HandlerResult:
    # Keep dispatch explicit so supported inbound event types stay obvious.
    if event.event_type == "post.created":
        return await handle_post_created(event, runtime)
    if event.event_type == "comment.created":
        return await handle_comment_created(event, runtime)
    if event.event_type == "follow.accepted":
        return await handle_follow_accepted(event, runtime)
    raise RuntimeError(f"Unsupported event type: {event.event_type}")


async def handle_post_created(event: ActivityPubEvent, runtime: Runtime) -> HandlerResult:
    if _is_discord_originated_echo(event, runtime):
        return HandlerResult(status="skipped", detail="discord-originated echo")

    # Route to every Discord channel subscribed to this community. In practice
    # there is usually one subscription, but the schema allows more.
    subscriptions = runtime.database.get_subscriptions_by_community(event.community_actor_id)
    if not subscriptions:
        return HandlerResult(status="skipped", detail="no subscriptions for this community")

    await runtime.bot.wait_until_bridge_ready()
    # Dedup guard: if the AP ID is already linked, another delivery already
    # processed this post successfully.
    if runtime.database.get_post_link_by_lemmy_post_ap_id(event.object.ap_id) is not None:
        return HandlerResult(status="skipped", detail="post already linked")

    for subscription in subscriptions:
        forum_channel = await runtime.bot.fetch_forum_channel(subscription.discord_channel_id)
        await create_discord_thread_for_activitypub_post(
            database=runtime.database,
            forum_channel=forum_channel,
            event=event,
        )
    return HandlerResult(status="processed", detail="post created")


async def handle_comment_created(event: ActivityPubEvent, runtime: Runtime) -> HandlerResult:
    if _is_discord_originated_echo(event, runtime):
        return HandlerResult(status="skipped", detail="discord-originated echo")

    # Skip early if no channel is subscribed to this community — avoids DB
    # writes for irrelevant communities.
    subscriptions = runtime.database.get_subscriptions_by_community(event.community_actor_id)
    if not subscriptions:
        return HandlerResult(status="skipped", detail="no subscriptions for this community")

    await runtime.bot.wait_until_bridge_ready()
    if runtime.database.get_comment_link_by_lemmy_comment_ap_id(event.object.ap_id) is not None:
        return HandlerResult(status="skipped", detail="comment already linked")

    # Comments are routed through their parent post link, which already carries
    # the correct discord_forum_thread_id. If the post arrived out of order or
    # was never processed, skip and let the caller decide whether to retry.
    post_link = runtime.database.get_post_link_by_lemmy_post_ap_id(event.object.post_ap_id or "")
    if post_link is None:
        logger.info("Skipping ActivityPub comment %s because post %s is not mapped yet", event.object.ap_id, event.object.post_ap_id)
        return HandlerResult(status="deferred", detail="parent post is not mapped yet")

    resolved_thread = await runtime.bot.get_thread_by_id(post_link.discord_forum_thread_id)
    await create_discord_message_for_activitypub_comment(
        database=runtime.database,
        thread=resolved_thread,
        event=event,
    )
    return HandlerResult(status="processed", detail="comment created")


async def handle_follow_accepted(
    event: FollowLifecycleEvent, runtime: Runtime
) -> HandlerResult:
    # Follow acceptance is pure subscription-state mutation, so it does not
    # touch Discord directly for routing state, but we still try to notify the
    # target forum channel after acceptance so moderators see the result in the
    # same place where they issued the subscribe command.
    subscription = runtime.database.get_subscription_by_follow_activity_id(
        event.object.follow_activity_id
    )
    if subscription is None:
        logger.info(
            "Skipping follow acceptance for unknown follow activity %s",
            event.object.follow_activity_id,
        )
        return HandlerResult(
            status="skipped", detail="follow activity is not mapped"
        )
    if subscription.status == "accepted":
        return HandlerResult(status="skipped", detail="subscription already accepted")

    runtime.database.mark_subscription_accepted_by_follow_activity_id(
        event.object.follow_activity_id
    )

    try:
        forum_channel = await runtime.bot.fetch_forum_channel(
            subscription.discord_channel_id
        )
        # The notification is best-effort: routing must not depend on Discord
        # message delivery after the subscription is already accepted.
        await forum_channel.send(
            f"Bridge follow for **{subscription.community_handle or subscription.lemmy_community_name or subscription.lemmy_community_actor_id}** was accepted. This channel is now federated."
        )
    except Exception:
        logger.exception(
            "Could not send follow acceptance notification for channel %s",
            subscription.discord_channel_id,
        )
        return HandlerResult(
            status="processed",
            detail="subscription accepted; notification failed",
        )

    return HandlerResult(
        status="processed",
        detail="subscription accepted and channel notified",
    )


def _is_discord_originated_echo(event: ActivityPubEvent, runtime: Runtime) -> bool:
    """Return whether an inbound AP event matches a prior Discord-originated publish."""
    # Outbound Discord publishes persist both activity_id and object_id, so
    # inbound loop suppression checks both keys before any Discord fanout.
    if runtime.database.get_message_mapping_by_object_id(event.object.ap_id) is not None:
        return True
    if runtime.database.get_message_mapping_by_activity_id(event.delivery_id) is not None:
        return True
    return False
