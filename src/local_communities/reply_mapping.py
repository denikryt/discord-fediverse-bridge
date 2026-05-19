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
    reference = getattr(message, "reference", None)
    referenced_id = getattr(reference, "message_id", None) if reference else None
    return resolve_root_or_mapped_reply(
        root_ap_object_id=getattr(thread_row, "ap_object_id"),
        referenced_message_id=referenced_id,
        lookup_mapped_message=database.get_local_community_message_by_discord_message_id,
    )


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
        return getattr(thread_row, "discord_starter_message_id")
    parent_message = database.get_local_community_message_by_ap_object_id(parent_ap_object_id)
    if parent_message is None:
        return None
    return getattr(parent_message, "discord_message_id")
