"""Inbound mapping helpers for shared community sync.

These helpers isolate the lookup and filtering rules used by inbound AP
delivery so the runtime reads as event orchestration instead of repository
plumbing.
"""

from __future__ import annotations


def get_accepted_subscriptions(database: object, community_actor_id: str) -> list[object]:
    """Return accepted subscriptions for one community actor id."""
    subscriptions = database.remote_subscriptions.get_subscriptions_by_community(community_actor_id)
    return [subscription for subscription in subscriptions if subscription.status == "accepted"]


def get_parent_message_group(database: object, parent_ap_id: str | None) -> object | None:
    """Return the parent message group for one inbound comment reply target."""
    if not parent_ap_id:
        return None
    return database.discord_fanout_groups.get_message_group_by_ap_object(parent_ap_id)


def needs_backfill(
    *,
    thread_group: object | None,
    community_actor_id: str,
    database: object,
) -> bool:
    """Return True when an inbound comment needs parent-post backfill.

    Backfill is required when the post thread group is missing entirely or when
    some accepted subscriptions still have no thread delivery row for the post.
    """
    accepted_channel_ids = {
        subscription.discord_channel_id
        for subscription in get_accepted_subscriptions(database, community_actor_id)
    }
    if not accepted_channel_ids:
        return False

    if thread_group is None:
        return True

    delivered_channel_ids = {
        delivery.discord_channel_id
        for delivery in database.discord_fanout_groups.get_thread_deliveries(thread_group.id)
    }
    return bool(accepted_channel_ids - delivered_channel_ids)

