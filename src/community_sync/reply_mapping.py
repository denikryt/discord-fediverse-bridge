"""Reply-chain mapping helpers for shared community sync.

These helpers keep Discord reply reference resolution out of the main runtime
so `CommunityRuntime` can stay focused on orchestration. They preserve the
current semantics for source replies, mirrored replies, starter replies, and
flat-send fallbacks.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from ..content_sync.inbound_references import build_message_reference


@dataclass(slots=True)
class ReplyContext:
    """Carry per-thread reply reference IDs resolved before fanout begins.

    `parent_message_group_id` is set when the source message replies to a known
    message group, so runtime code can persist the FK on the new message group.

    `per_thread_references` maps mirror thread id -> Discord message id to use
    as the reference when sending into that thread. A `None` value means flat
    send for that thread.
    """

    parent_message_group_id: int | None
    per_thread_references: dict[int, int | None]

    def get_reference_for_thread(self, thread_id: int) -> int | None:
        """Return the Discord message ID to reference in one target thread."""
        return self.per_thread_references.get(thread_id)


def resolve_reply_context(
    database: object,
    message: object,
    thread_group: object,
    sibling_deliveries: list[object],
) -> ReplyContext:
    """Resolve per-thread reply references for one outbound Discord message.

    The current behavior is preserved exactly:

    - no source reference -> flat send everywhere
    - reply to any starter message -> each sibling references its own starter
    - reply to a known mirrored message -> each sibling references its own copy
    - unknown reference -> flat fallback for all siblings
    """
    reference = getattr(message, "reference", None)
    referenced_id = getattr(reference, "message_id", None) if reference else None

    if referenced_id is None:
        return ReplyContext(
            parent_message_group_id=None,
            per_thread_references={d.discord_thread_id: None for d in sibling_deliveries},
        )

    thread_deliveries = database.discord_fanout_groups.get_thread_deliveries(thread_group.id)
    for delivery in thread_deliveries:
        if referenced_id == delivery.discord_starter_message_id:
            return ReplyContext(
                parent_message_group_id=None,
                per_thread_references={
                    d.discord_thread_id: d.discord_starter_message_id
                    for d in sibling_deliveries
                },
            )

    parent_group = database.discord_fanout_groups.get_message_group_by_delivered_message(referenced_id)
    if parent_group is None:
        return ReplyContext(
            parent_message_group_id=None,
            per_thread_references={d.discord_thread_id: None for d in sibling_deliveries},
        )

    per_thread: dict[int, int | None] = {}
    for delivery in sibling_deliveries:
        mirror_delivery = database.discord_fanout_groups.get_message_delivery_in_thread(
            parent_group.id, delivery.discord_thread_id
        )
        per_thread[delivery.discord_thread_id] = (
            mirror_delivery.discord_message_id if mirror_delivery else None
        )

    return ReplyContext(
        parent_message_group_id=parent_group.id,
        per_thread_references=per_thread,
    )


def resolve_inbound_reference(
    database: object,
    parent_group: object | None,
    thread_delivery: object,
) -> object | None:
    """Resolve one inbound Discord reply reference from message-group mappings.

    Returns `None` for root comments or when the parent message has no delivery
    in this specific thread. That preserves the current best-effort flat-send
    fallback used by the inbound comment flow.
    """
    if parent_group is None:
        return None

    delivery = database.discord_fanout_groups.get_message_delivery_in_thread(
        parent_group.id, thread_delivery.discord_thread_id
    )
    if delivery is None:
        return None

    # Build the concrete discord.py reference through the shared helper so both
    # bridge modes keep the same `fail_if_not_exists=False` reply behavior.
    return build_message_reference(
        discord_thread=SimpleNamespace(id=thread_delivery.discord_thread_id),
        message_id=delivery.discord_message_id,
    )
