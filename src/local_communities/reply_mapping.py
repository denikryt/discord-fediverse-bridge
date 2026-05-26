"""Reply-target resolution helpers for local-community publish and inbound flows.

Reply mapping is correctness-critical because local-community comments must
preserve parent structure across ActivityPub and each concrete Discord surface.
Stage 4 adds local-subscriber source surfaces, so outbound resolution can no
longer assume the host surface is always the source context.
"""

from __future__ import annotations

from ..db import Database
from ..content_sync.reply_mapping import ResolvedReplyTarget, resolve_root_or_mapped_reply


def resolve_outbound_reply_context(
    *,
    database: Database,
    thread_row: object,
    message: object,
) -> ResolvedReplyTarget:
    """Resolve which AP object an outbound host-forum reply should target."""
    host_surface = database.get_host_local_community_thread_surface(getattr(thread_row, "id"))
    return resolve_outbound_reply_context_for_surface(
        database=database,
        thread_row=thread_row,
        source_thread_surface=host_surface,
        message=message,
    )


def resolve_outbound_reply_context_for_surface(
    *,
    database: Database,
    thread_row: object,
    source_thread_surface: object | None,
    message: object,
) -> ResolvedReplyTarget:
    """Resolve an outbound reply target inside one concrete source surface.

    The `source_thread_surface` constraint prevents a local subscriber reply
    from accidentally resolving a parent message surface that belongs to the
    host or a sibling subscriber forum.  Unknown references preserve the prior
    host behavior by falling back to the root AP post object.
    """

    def lookup_mapped_message(discord_message_id: int) -> object | None:
        message_surface = database.get_local_community_message_surface_by_discord_message_id(discord_message_id)
        if message_surface is None:
            return None
        if source_thread_surface is not None and getattr(message_surface, "local_community_thread_surface_id") != getattr(source_thread_surface, "id"):
            return None
        return database.get_local_community_message_for_surface(message_surface.id)

    reference = getattr(message, "reference", None)
    referenced_id = getattr(reference, "message_id", None) if reference else None
    resolved = resolve_root_or_mapped_reply(
        root_ap_object_id=getattr(thread_row, "ap_object_id"),
        referenced_message_id=referenced_id,
        lookup_mapped_message=lookup_mapped_message,
    )
    if resolved.parent_discord_message_id is None and source_thread_surface is not None:
        # Root replies on any source surface should reference that surface's
        # starter message, not the host starter, so later fanout can map parent
        # surfaces target-by-target.
        resolved = ResolvedReplyTarget(
            parent_ap_object_id=resolved.parent_ap_object_id,
            parent_discord_message_id=getattr(source_thread_surface, "discord_starter_message_id"),
        )
    return resolved


def resolve_inbound_reply_target(
    *,
    database: Database,
    parent_ap_object_id: str | None,
    thread_row: object,
) -> int | None:
    """Resolve which host Discord message an inbound remote reply should reference."""
    if parent_ap_object_id is None or parent_ap_object_id == getattr(thread_row, "ap_object_id"):
        thread_surface = database.get_host_local_community_thread_surface(getattr(thread_row, "id"))
        if thread_surface is None:
            return None
        return getattr(thread_surface, "discord_starter_message_id")
    parent_message = database.get_local_community_message_by_ap_object_id(parent_ap_object_id)
    if parent_message is None:
        return None
    message_surface = database.get_host_local_community_message_surface(getattr(parent_message, "id"))
    if message_surface is None:
        return None
    return getattr(message_surface, "discord_message_id")
