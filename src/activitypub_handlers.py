"""Handlers for inbound ActivityPub events delivered by the Fedify gateway.

Each handler receives a typed event model and a Runtime, performs all DB
mutations, and returns a HandlerResult that the HTTP layer records as a receipt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .activitypub_models import (
    ActivityPubEvent,
    BridgeGatewayEvent,
    FollowLifecycleEvent,
)
from .community_sync.inbound_mapping import get_accepted_subscriptions
from .local_communities.inbound_mapping import resolve_local_community_by_actor_url
from .federation_policy import is_instance_allowed
from .runtime import Runtime

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class HandlerResult:
    """Carry the processing outcome of one inbound ActivityPub event.

    Handlers return explicit status so the HTTP receipt layer can record
    whether the event was processed, skipped, or needs a retry.
    """

    # status values: processed | skipped
    status: str
    detail: str


async def dispatch_activitypub_event(
    event: BridgeGatewayEvent, runtime: Runtime
) -> HandlerResult:
    """Route one inbound ActivityPub event to the correct handler.

    follow.accepted is exempt from the allowlist check because it is a
    lifecycle response to our own outbound Follow and must always be processed.
    All other event types are filtered against the federation allowlist before
    any handler logic runs.
    """
    # follow.accepted and local.follow_requested are lifecycle/control events,
    # not remote community content fanout, so they bypass the normal allowlist.
    if event.event_type not in {"follow.accepted", "local.follow_requested", "local.unfollow_requested"}:
        # settings may be absent in test runtimes that pre-date allowlist support;
        # treat a missing settings as an empty allowlist (allow all).
        allowlist = getattr(getattr(runtime, "settings", None), "federation_allowlist", [])
        if not is_instance_allowed(_allowlist_subject(event, runtime), allowlist):
            logger.debug(
                "Skipping %s from non-allowlisted instance: %s",
                event.event_type,
                _allowlist_subject(event, runtime),
            )
            return HandlerResult(status="skipped", detail="instance not in allowlist")

    # Keep dispatch explicit so supported inbound event types stay obvious.
    if event.event_type == "post.created":
        return await handle_post_created(event, runtime)
    if event.event_type == "comment.created":
        return await handle_comment_created(event, runtime)
    if event.event_type == "post.updated":
        if _is_local_community_target(event, runtime):
            return await runtime.local_community_runtime.handle_inbound_post_update(event, runtime)
        return await runtime.community_runtime.handle_inbound_post_update(event, runtime)
    if event.event_type == "post.deleted":
        if _is_local_community_target(event, runtime):
            return await runtime.local_community_runtime.handle_inbound_post_delete(event, runtime)
        return await runtime.community_runtime.handle_inbound_post_delete(event, runtime)
    if event.event_type == "comment.updated":
        if _is_local_community_target(event, runtime):
            return await runtime.local_community_runtime.handle_inbound_comment_update(event, runtime)
        return await runtime.community_runtime.handle_inbound_comment_update(event, runtime)
    if event.event_type == "comment.deleted":
        if _is_local_community_target(event, runtime):
            return await runtime.local_community_runtime.handle_inbound_comment_delete(event, runtime)
        return await runtime.community_runtime.handle_inbound_comment_delete(event, runtime)
    if event.event_type == "follow.accepted":
        return await handle_follow_accepted(event, runtime)
    if event.event_type == "local.follow_requested":
        return await runtime.local_community_runtime.handle_follow_request(
            local_community_actor_id=event.community_actor_id,
            remote_actor_id=event.actor_id,
            remote_inbox_url=event.object.remote_inbox_url,
            follow_activity_id=event.object.follow_activity_id,
        )
    if event.event_type == "local.unfollow_requested":
        return await runtime.local_community_runtime.handle_unfollow_request(
            local_community_actor_id=event.community_actor_id,
            remote_actor_id=event.actor_id,
            follow_activity_id=event.object.follow_activity_id,
        )
    raise RuntimeError(f"Unsupported event type: {event.event_type}")


async def handle_post_created(event: ActivityPubEvent, runtime: Runtime) -> HandlerResult:
    """Route one inbound ActivityPub post through CommunityRuntime.

    Echo suppression is applied here before routing so the check is not
    duplicated inside CommunityRuntime. Also promotes pending follows to
    accepted when the instance starts delivering without sending Accept first.
    """
    if _is_discord_originated_echo(event, runtime):
        return HandlerResult(status="skipped", detail="discord-originated echo")
    if _is_local_community_target(event, runtime):
        return await runtime.local_community_runtime.handle_inbound_post(event, runtime)
    await _maybe_implicit_accept(event.community_actor_id, runtime)
    if _should_skip_unsubscribed_remote_create(event, runtime):
        logger.info(
            "Skipping inbound %s for unsubscribed community %s activity=%s object=%s post=%s parent=%s",
            event.event_type, event.community_actor_id, event.delivery_id,
            event.object.ap_id, event.object.post_ap_id, event.object.parent_ap_id,
        )
        return HandlerResult(status="skipped", detail="no subscriptions for this community")
    return await runtime.community_runtime.handle_inbound_post(event, runtime)


async def handle_comment_created(event: ActivityPubEvent, runtime: Runtime) -> HandlerResult:
    """Route one inbound ActivityPub comment through CommunityRuntime.

    Echo suppression is applied here before routing so the check is not
    duplicated inside CommunityRuntime. Also promotes pending follows to
    accepted when the instance starts delivering without sending Accept first.
    """
    if _is_discord_originated_echo(event, runtime):
        return HandlerResult(status="skipped", detail="discord-originated echo")
    if _is_local_community_target(event, runtime):
        return await runtime.local_community_runtime.handle_inbound_comment(event, runtime)
    await _maybe_implicit_accept(event.community_actor_id, runtime)
    if _should_skip_unsubscribed_remote_create(event, runtime):
        logger.info(
            "Skipping inbound %s for unsubscribed community %s activity=%s object=%s post=%s parent=%s",
            event.event_type, event.community_actor_id, event.delivery_id,
            event.object.ap_id, event.object.post_ap_id, event.object.parent_ap_id,
        )
        return HandlerResult(status="skipped", detail="no subscriptions for this community")
    return await runtime.community_runtime.handle_inbound_comment(event, runtime)


async def handle_follow_accepted(
    event: FollowLifecycleEvent, runtime: Runtime
) -> HandlerResult:
    """Process an Accept(Follow) from a remote Lemmy instance.

    Marks the BridgeActorFollow row accepted, which in turn marks all pending
    ChannelCommunitySubscription rows for the same community accepted. Then
    DMs every Discord user who initiated one of those subscriptions so they see
    confirmation in the channel where they ran /subscribe-channel.
    """
    follow_activity_id = event.object.follow_activity_id

    # Accept(Follow) is now valid only when it matches the bridge-level
    # lifecycle row.  Older direct subscription acceptance has been removed so
    # a missing BridgeActorFollow is treated as a stale or unknown remote reply,
    # not as permission to mutate channel subscription rows directly.
    bridge_follow = runtime.database.get_bridge_actor_follow_by_follow_activity_id(
        follow_activity_id
    )

    if bridge_follow is None:
        logger.info(
            "Skipping follow acceptance for unknown bridge follow activity %s",
            follow_activity_id,
        )
        return HandlerResult(status="skipped", detail="bridge follow activity is not mapped")

    if bridge_follow.status == "accepted":
        return HandlerResult(status="skipped", detail="bridge follow already accepted")

    # Mark the bridge-actor follow and all pending channel rows accepted atomically.
    # get_pending_channel_subscriptions_for_community must be called BEFORE
    # mark_bridge_actor_follow_accepted so we capture the pending rows that need DMs.
    community_actor_id = bridge_follow.community_actor_id
    pending_subs = runtime.database.get_pending_channel_subscriptions_for_community(
        community_actor_id
    )
    runtime.database.mark_bridge_actor_follow_accepted(community_actor_id)

    # DM every Discord user who was waiting on this community.
    for sub in pending_subs:
        await _notify_channel_accepted(sub, runtime)

    return HandlerResult(
        status="processed",
        detail=f"bridge follow accepted; {len(pending_subs)} channel(s) notified",
    )


async def _maybe_implicit_accept(
    community_actor_id: str, runtime: Runtime
) -> None:
    """Promote a pending bridge-actor follow to accepted when inbound events arrive.

    Some Lemmy instances start delivering events without ever sending an explicit
    Accept(Follow). This function detects that case: if we receive a post or
    comment from a community whose bridge-actor follow is still pending, we treat
    the delivery itself as implicit acceptance and activate all waiting channels.
    """
    bridge_follow = runtime.database.get_bridge_actor_follow(community_actor_id)
    if bridge_follow is None or bridge_follow.status != "pending":
        # Either already accepted/failed or no follow row at all — nothing to promote.
        return

    logger.info(
        "Implicit accept: received inbound event for pending follow on %s; "
        "promoting to accepted",
        community_actor_id,
    )

    # Capture pending subs before marking accepted so DMs can be sent.
    pending_subs = runtime.database.get_pending_channel_subscriptions_for_community(
        community_actor_id
    )
    runtime.database.mark_bridge_actor_follow_accepted(community_actor_id)

    for sub in pending_subs:
        await _notify_channel_accepted(sub, runtime)


def _should_skip_unsubscribed_remote_create(
    event: ActivityPubEvent,
    runtime: Runtime,
) -> bool:
    """Return whether one remote Create event is irrelevant after unsubscribe.

    The ActivityPub gateway still acknowledges valid inbox deliveries. This
    guard only prevents local side effects when remote community content has no
    accepted subscription and no mapped thread context that still belongs to the
    bridge.
    """

    # Accepted subscriptions always keep the normal inbound path active.
    accepted = get_accepted_subscriptions(runtime.database, event.community_actor_id)
    if accepted:
        return False

    # `_maybe_implicit_accept()` runs before this helper. Keep the pending-follow
    # allowance here as a defensive rule so a future call-site reorder does not
    # accidentally skip valid content from a community that is still activating.
    bridge_follow = runtime.database.get_bridge_actor_follow(event.community_actor_id)
    if bridge_follow is not None and bridge_follow.status == "pending":
        return False

    if event.event_type == "post.created":
        # Unsubscribed posts are only relevant if they already map to a stored
        # bridge thread group. A brand-new remote post after unsubscribe should
        # be acknowledged and ignored locally.
        return runtime.database.get_thread_group_by_ap_object(event.object.ap_id) is None

    if event.event_type == "comment.created":
        # Comments that still belong to a mapped thread or a mapped parent
        # message remain actionable even after unsubscribe. Only orphan comments
        # with no mapped bridge context are skipped here.
        if (
            event.object.post_ap_id
            and runtime.database.get_thread_group_by_ap_object(event.object.post_ap_id)
            is not None
        ):
            return False
        if (
            event.object.parent_ap_id
            and runtime.database.get_message_group_by_ap_object(event.object.parent_ap_id)
            is not None
        ):
            return False
        return True

    # This helper is intentionally limited to remote Create events only.
    return False


async def _notify_channel_accepted(subscription: object, runtime: Runtime) -> None:
    """DM the Discord user who initiated a subscription that it is now active.

    Failures are caught and logged rather than propagated so a DM failure does
    not prevent the subscription from being marked accepted.
    """
    try:
        community_label = (
            getattr(subscription, "community_handle", None)
            or getattr(subscription, "lemmy_community_name", None)
            or getattr(subscription, "lemmy_community_actor_id", "unknown")
        )
        discord_channel_id = getattr(subscription, "discord_channel_id", None)
        initiator_id = getattr(subscription, "initiated_by_discord_user_id", None)

        if initiator_id is not None:
            user = await runtime.bot.fetch_user(int(initiator_id))
            await user.send(
                f"Your bridge follow for **{community_label}** was accepted. "
                f"<#{discord_channel_id}> is now federated."
            )
        else:
            logger.warning(
                "No initiator recorded for subscription %s, skipping DM notification",
                discord_channel_id,
            )
    except Exception:
        logger.exception(
            "Could not send follow acceptance notification for channel %s",
            getattr(subscription, "discord_channel_id", "unknown"),
        )


def _is_discord_originated_echo(event: ActivityPubEvent, runtime: Runtime) -> bool:
    """Return whether an inbound AP event matches a prior Discord-originated publish."""
    # Outbound Discord publishes persist both activity_id and object_id, so
    # inbound loop suppression checks both keys before any Discord fanout.
    if runtime.database.get_message_mapping_by_object_id(event.object.ap_id) is not None:
        return True
    if runtime.database.get_message_mapping_by_activity_id(event.delivery_id) is not None:
        return True
    # Lemmy re-wraps our outbound activities in an Announce with a new ap_id, so
    # the object_id check above misses the echo. If the actor is one of our own
    # registered users, the activity originated here and must be suppressed.
    if runtime.database.get_user_by_actor_url(event.actor_id) is not None:
        return True
    return False


def _is_local_community_target(event: ActivityPubEvent, runtime: Runtime) -> bool:
    """Return whether the event targets one local community actor we own."""
    return (
        resolve_local_community_by_actor_url(runtime.database, event.community_actor_id)
        is not None
    )


def _allowlist_subject(event: BridgeGatewayEvent, runtime: Runtime) -> str:
    """Return the remote URL whose instance should be checked against the allowlist."""
    if (
        hasattr(event, "community_actor_id")
        and resolve_local_community_by_actor_url(runtime.database, event.community_actor_id) is not None
    ):
        return getattr(event, "actor_id")
    return getattr(event, "community_actor_id")
