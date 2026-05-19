"""Shared content-sync helpers used by both bridge modes.

The package owns reusable content preparation, generic AP publish persistence,
reply-target contracts, inbound Discord references, and content-level
edit/delete helpers. It intentionally does not own routing policy or
mode-specific mapping tables.
"""

from .edit_delete import (
    edit_discord_message,
    mark_discord_message_deleted,
    resolve_published_object_for_discord_message,
)
from .inbound_references import build_message_reference
from .outbound_publish import (
    build_discord_comment_body,
    build_discord_post_title,
    resolve_registered_user,
)
from .persistence import persist_publish_artifacts
from .reply_mapping import ResolvedReplyTarget, resolve_root_or_mapped_reply

__all__ = [
    "ResolvedReplyTarget",
    "build_discord_comment_body",
    "build_discord_post_title",
    "build_message_reference",
    "edit_discord_message",
    "mark_discord_message_deleted",
    "persist_publish_artifacts",
    "resolve_published_object_for_discord_message",
    "resolve_registered_user",
    "resolve_root_or_mapped_reply",
]
