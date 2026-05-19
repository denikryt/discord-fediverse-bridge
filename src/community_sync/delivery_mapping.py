"""Named delivery and group lookup helpers for shared community sync.

The runtime uses these helpers to make group/delivery intent explicit instead
of repeating raw repository calls inline.
"""

from __future__ import annotations


def get_thread_group_for_source_thread(database: object, thread_id: int) -> object | None:
    """Return the canonical thread group for one source Discord thread."""
    return database.get_thread_group_by_source_thread(thread_id)


def get_thread_group_for_any_thread(database: object, thread_id: int) -> object | None:
    """Return the canonical thread group for any mapped Discord thread."""
    return database.get_thread_group_by_any_thread(thread_id)


def get_thread_group_for_ap_object(database: object, ap_object_id: str) -> object | None:
    """Return the canonical thread group for one AP post object id."""
    return database.get_thread_group_by_ap_object(ap_object_id)


def get_message_group_for_source_message(database: object, message_id: int) -> object | None:
    """Return the canonical message group for one source Discord message."""
    return database.get_message_group_by_source_message(message_id)


def get_message_group_for_ap_object(database: object, ap_object_id: str) -> object | None:
    """Return the canonical message group for one AP comment object id."""
    return database.get_message_group_by_ap_object(ap_object_id)


def get_message_group_for_delivered_message(database: object, message_id: int) -> object | None:
    """Return the canonical message group for one delivered Discord message id."""
    return database.get_message_group_by_delivered_message(message_id)


def get_sibling_thread_deliveries(
    database: object,
    *,
    thread_group_id: int,
    source_thread_id: int,
) -> list[object]:
    """Return every mapped thread delivery except the originating thread."""
    return [
        delivery
        for delivery in database.get_thread_deliveries(thread_group_id)
        if delivery.discord_thread_id != source_thread_id
    ]


def get_message_delivery_for_thread(
    database: object,
    *,
    message_group_id: int,
    thread_id: int,
) -> object | None:
    """Return the delivery row for one message group in one Discord thread."""
    return database.get_message_delivery_in_thread(message_group_id, thread_id)

