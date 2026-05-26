"""Reply-target resolution helpers for local-community publish and inbound flows.

Reply mapping is correctness-critical because local-community comments must
preserve the same parent thread/message structure on both Discord and the
federated side. The local-community mode reuses the shared root-or-mapped
contract while keeping its own table lookups.
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
    """Resolve which AP object an outbound Discord reply should target.

    Root replies target the thread's post object. Replies to known mapped local
    messages target that message's AP comment object instead.
    """
    # Stage 2 resolves the parent through the message surface table so reply
    # ownership no longer depends on Discord ids living on canonical rows.
    def lookup_mapped_message(discord_message_id: int) -> object | None:
        message_surface = database.get_local_community_message_surface_by_discord_message_id(
            discord_message_id
        )
        if message_surface is None:
            return None
        return database.get_local_community_message_for_surface(message_surface.id)

    reference = getattr(message, "reference", None)
    referenced_id = getattr(reference, "message_id", None) if reference else None
    resolved = resolve_root_or_mapped_reply(
        root_ap_object_id=getattr(thread_row, "ap_object_id"),
        referenced_message_id=referenced_id,
        lookup_mapped_message=lookup_mapped_message,
    )
    if resolved.parent_discord_message_id is None:
        thread_surface = database.get_host_local_community_thread_surface(
            getattr(thread_row, "id")
        )
        if thread_surface is not None:
            # Stage 2 keeps root-reply ownership explicit on the message
            # surface so later per-surface sync can distinguish starter-vs-reply
            # without reaching back into removed canonical Discord columns.
            resolved = ResolvedReplyTarget(
                parent_ap_object_id=resolved.parent_ap_object_id,
                parent_discord_message_id=getattr(
                    thread_surface, "discord_starter_message_id"
                ),
            )
    return resolved


def resolve_inbound_reply_target(
    *,
    database: Database,
    parent_ap_object_id: str | None,
    thread_row: object,
) -> int | None:
    """Resolve which Discord message an inbound remote reply should reference.

    Replies to the post root return the starter message id, while replies to a
    known mapped comment return that mapped Discord message id.
    """
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
