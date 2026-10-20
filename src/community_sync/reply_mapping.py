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
from ..discord_legacy_projection import discord_id_to_legacy_int
from ..platform_types import MessageCreated, PlatformOperationError


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
    """Resolve the legacy Discord reference through the shared DB mapping logic."""
    reference = getattr(message, 'reference', None)
    referenced_id = getattr(reference, 'message_id', None) if reference else None
    return _resolve_reference_context(database, referenced_id, thread_group, sibling_deliveries)


def resolve_normalized_reply_context(
    database: object, event: MessageCreated,
    thread_group: object, sibling_deliveries: list[object],
) -> ReplyContext:
    """Resolve a normalized forum reply under verified physical and canonical ownership.

    A cross-thread parent ID or parent mapped to another canonical thread group
    cannot become the newly published comment's parent FK or a remote reference.
    Unknown IDs retain the established flat-send behavior.
    """
    thread = event.thread_context
    if (event.connector_key != 'discord-primary' or thread is None
        or event.parent_channel is None or event.parent_channel.kind != 'forum'
        or thread.channel.kind != 'forum'
        or thread.channel != event.parent_channel
        or thread.channel.external_guild_id != event.external_guild_id
        or event.message.external_container_id != thread.external_thread_id):
        raise PlatformOperationError('invalid_request', 'unverified community reply source')
    thread_id = discord_id_to_legacy_int(thread)
    forum_id = discord_id_to_legacy_int(thread, component='parent')
    # Only a delivery belonging to this canonical group authorizes resolving
    # references from the claimed physical source thread.
    group_deliveries = database.discord_fanout_groups.get_thread_deliveries(thread_group.id)
    if not any(d.discord_thread_id == thread_id and d.discord_channel_id == forum_id
               for d in group_deliveries):
        raise PlatformOperationError('invalid_request', 'source thread not in canonical group')
    if any(d.thread_group_id != thread_group.id for d in sibling_deliveries):
        raise PlatformOperationError('invalid_request', 'sibling belongs to another group')
    reference_id = None
    if event.reply_to is not None:
        if (event.reply_to.connector_key != event.connector_key
            or event.reply_to.external_container_id != thread.external_thread_id):
            raise PlatformOperationError('invalid_request', 'cross-thread reply parent')
        reference_id = discord_id_to_legacy_int(event.reply_to)
    return _resolve_reference_context(
        database, reference_id, thread_group, sibling_deliveries,
        source_thread_id=thread_id,
    )


def _resolve_reference_context(
    database: object, referenced_id: int | None,
    thread_group: object, sibling_deliveries: list[object],
    *, source_thread_id: int | None = None,
) -> ReplyContext:
    """Share SQLite reply-chain resolution without constructing SDK-shaped proxies."""
    if referenced_id is None:
        return ReplyContext(
            parent_message_group_id=None,
            per_thread_references={
                d.discord_thread_id: None for d in sibling_deliveries
            },
        )

    thread_deliveries = database.discord_fanout_groups.get_thread_deliveries(
        thread_group.id
    )
    for delivery in thread_deliveries:
        if (source_thread_id is not None and delivery.discord_thread_id != source_thread_id):
            continue
        if referenced_id == delivery.discord_starter_message_id:
            return ReplyContext(
                parent_message_group_id=None,
                per_thread_references={
                    d.discord_thread_id: d.discord_starter_message_id
                    for d in sibling_deliveries
                },
            )

    parent_group = (
        database.discord_fanout_groups.get_message_group_by_delivered_message(
            referenced_id
        )
    )
    # A message ID from another physical thread must not become a canonical
    # parent merely because it has a persisted group somewhere in the bridge.
    valid_parent = parent_group is not None
    if valid_parent and source_thread_id is not None:
        physical = database.discord_fanout_groups.get_message_delivery_by_message(referenced_id)
        valid_parent = (
            parent_group.thread_group_id == thread_group.id
            and physical is not None and physical.discord_thread_id == source_thread_id
        )
    if not valid_parent:
        return ReplyContext(
            parent_message_group_id=None,
            per_thread_references={
                d.discord_thread_id: None for d in sibling_deliveries
            },
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
