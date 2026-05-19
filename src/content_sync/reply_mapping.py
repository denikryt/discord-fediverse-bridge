"""Shared reply-target contracts for outbound Discord replies.

The two bridge modes keep different canonical mapping tables, but the root
semantics are the same: a Discord reply either targets the post root, a known
mapped parent comment, or falls back safely when the reference is unknown.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ResolvedReplyTarget:
    """Carry the parent identifiers chosen for one outbound Discord reply."""

    parent_ap_object_id: str
    parent_discord_message_id: int | None


def resolve_root_or_mapped_reply(
    *,
    root_ap_object_id: str,
    referenced_message_id: int | None,
    lookup_mapped_message: object,
) -> ResolvedReplyTarget:
    """Resolve one outbound reply against a root object and a mapping lookup.

    `lookup_mapped_message` is a callable that receives a referenced Discord
    message id and returns the mode-specific canonical row for that message, or
    `None` when the reference is unknown in that mode.
    """
    if referenced_message_id is None:
        return ResolvedReplyTarget(
            parent_ap_object_id=root_ap_object_id,
            parent_discord_message_id=None,
        )

    mapped_message = lookup_mapped_message(referenced_message_id)
    if mapped_message is None:
        return ResolvedReplyTarget(
            parent_ap_object_id=root_ap_object_id,
            parent_discord_message_id=referenced_message_id,
        )

    return ResolvedReplyTarget(
        parent_ap_object_id=getattr(mapped_message, "ap_object_id"),
        parent_discord_message_id=referenced_message_id,
    )
