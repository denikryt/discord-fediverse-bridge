"""Reply-target resolution helpers for local-community publish and inbound flows.

Reply mapping is correctness-critical because local-community comments must
preserve the same parent thread/message structure on both Discord and the
federated side.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..db import Database


@dataclass(slots=True)
class LocalReplyContext:
    """Carry the resolved parent identifiers for one local-community reply."""

    parent_ap_object_id: str
    parent_discord_message_id: int | None


def resolve_outbound_reply_context(
    *,
    database: Database,
    thread_row: object,
    message: object,
) -> LocalReplyContext:
    """Resolve which AP object an outbound Discord reply should target.

    Root replies target the thread's post object. Replies to known mapped local
    messages target that message's AP comment object instead.
    """
    reference = getattr(message, "reference", None)
    referenced_id = getattr(reference, "message_id", None) if reference else None
    if referenced_id is None:
        return LocalReplyContext(
            parent_ap_object_id=getattr(thread_row, "ap_object_id"),
            parent_discord_message_id=None,
        )

    mapped_message = database.get_local_community_message_by_discord_message_id(referenced_id)
    if mapped_message is None:
        return LocalReplyContext(
            parent_ap_object_id=getattr(thread_row, "ap_object_id"),
            parent_discord_message_id=referenced_id,
        )
    return LocalReplyContext(
        parent_ap_object_id=getattr(mapped_message, "ap_object_id"),
        parent_discord_message_id=referenced_id,
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
