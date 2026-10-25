"""Named delivery and group lookup helpers for shared community sync.

The runtime uses these helpers to make group/delivery intent explicit instead
of repeating raw repository calls inline.
"""

from __future__ import annotations

from ..models import ChannelSurface, PlatformConnector
from ..platform_types import ChannelRef, MessageRef, ThreadRef


def remote_thread_receipt(database: object, row: object) -> tuple[ThreadRef, MessageRef] | None:
    """Resolve a remote physical thread/starter from authoritative connector/surface IDs.

    A historic row's nullable Discord columns are never evidence of current
    ownership. A reused forum, wrong connector or changed guild is rejected.
    """
    with database.session() as session:
        surface = session.get(ChannelSurface, row.channel_surface_id)
        connector = session.get(PlatformConnector, row.connector_id)
        if (surface is None or connector is None or surface.connector_id != row.connector_id
            or surface.kind != 'forum' or not surface.external_guild_id
            or connector.connector_key != 'discord-primary'):
            return None
        forum = ChannelRef(connector.connector_key, surface.external_channel_id,
                           'forum', surface.external_guild_id)
        thread = ThreadRef(forum, row.external_thread_id)
        starter = MessageRef(connector.connector_key, row.external_thread_id,
                             row.external_starter_message_id)
        return thread, starter


def remote_thread_is_current(database: object, row: object, community_actor_id: str) -> bool:
    """Only the exact currently accepted subscription can use a historical copy."""
    refs = remote_thread_receipt(database, row)
    if refs is None:
        return False
    forum = refs[0].channel
    try:
        channel_id = int(forum.external_channel_id)
        if channel_id <= 0 or str(channel_id) != forum.external_channel_id:
            return False
    except (ValueError, TypeError):
        return False
    subscription = database.remote_subscriptions.get_subscription_by_channel(channel_id)
    placement = database.platform_surfaces.resolve_claim(
        forum.connector_key, forum.external_channel_id,
    )
    return bool(subscription is not None and placement is not None
        and subscription.status == 'accepted'
        and subscription.lemmy_community_actor_id == community_actor_id
        and placement.id == row.channel_surface_id
        and placement.remote_subscription_id == subscription.id
        and str(subscription.discord_guild_id) == forum.external_guild_id)



def get_thread_group_for_source_thread(
    database: object, thread_id: int
) -> object | None:
    """Return the canonical thread group for one source Discord thread."""
    return database.discord_fanout_groups.get_thread_group_by_source_thread(thread_id)


def get_thread_group_for_any_thread(database: object, thread_id: int | ThreadRef) -> object | None:
    """Resolve a normalized physical thread by exact placement; support old raw callers."""
    if isinstance(thread_id, ThreadRef):
        return database.discord_fanout_groups.get_thread_group_by_ref(thread_id)
    return database.discord_fanout_groups.get_thread_group_by_any_thread(thread_id)


def get_thread_group_for_ap_object(database: object, ap_object_id: str) -> object | None:
    """Return the original canonical post for a verified AP object identity."""
    return database.discord_fanout_groups.get_thread_group_by_ap_object(ap_object_id)


def get_message_group_for_source_message(
    database: object, message_id: int
) -> object | None:
    """Return the canonical message group for one source Discord message."""
    return database.discord_fanout_groups.get_message_group_by_source_message(
        message_id
    )


def get_message_group_for_ap_object(
    database: object, ap_object_id: str
) -> object | None:
    """Return the canonical message group for one AP comment object id."""
    return database.discord_fanout_groups.get_message_group_by_ap_object(ap_object_id)


def get_message_group_for_delivered_message(
    database: object, message_id: int
) -> object | None:
    """Return the canonical message group for one delivered Discord message id."""
    return database.discord_fanout_groups.get_message_group_by_delivered_message(
        message_id
    )


def get_sibling_thread_deliveries(
    database: object,
    *,
    thread_group_id: int,
    source_thread_id: int | None = None,
    source_thread: ThreadRef | None = None,
) -> list[object]:
    """Enumerate only exact currently authorized copies, including typed-only receipts.

    The normalized path supplies a ThreadRef; raw old SDK paths may still pass
    the integer. A nullable Discord projection is never a reason to discard a
    verified coordinator receipt or to authorize a stale/rebound forum.
    """
    group = database.discord_fanout_groups.get_thread_group_by_id(thread_group_id)
    if group is None:
        return []
    result = []
    for delivery in database.discord_fanout_groups.get_thread_deliveries(thread_group_id):
        refs = remote_thread_receipt(database, delivery)
        if refs is None or not remote_thread_is_current(database, delivery, group.community_actor_id):
            continue
        physical_thread = refs[0]
        if (source_thread is not None and physical_thread == source_thread
            or source_thread is None and source_thread_id is not None
            and physical_thread.external_thread_id == str(source_thread_id)):
            continue
        result.append(delivery)
    return result


def get_message_delivery_for_thread(
    database: object,
    *,
    message_group_id: int,
    thread_id: int,
) -> object | None:
    """Return the delivery row for one message group in one Discord thread."""
    return database.discord_fanout_groups.get_message_delivery_in_thread(
        message_group_id, thread_id
    )
