"""Discord-facing rendering helpers for local-community inbound ActivityPub.

The helpers in this module are pure formatting functions.  They preserve the
runtime's existing use of the shared Lemmy-to-Discord formatting layer while
making inbound create and mutation handlers independent from the facade module.
"""

from __future__ import annotations

from ..formatting import (
    format_lemmy_comment_for_discord,
    format_lemmy_post_for_discord,
    normalize_text,
)


def discord_author_display_name(message: object) -> str | None:
    """Return the best available Discord-side author label for local fanout."""
    author = getattr(message, "author", None)
    return getattr(author, "display_name", None) or getattr(author, "name", None)


def unpack_created_thread(created: object) -> tuple[object, object]:
    """Normalize both supported Discord `create_thread()` return shapes."""
    if isinstance(created, tuple):
        return created[0], created[1]
    return getattr(created, "thread"), getattr(created, "message")


def format_inbound_post_body(event: object) -> str:
    """Render one inbound remote post using the shared Discord-facing format."""
    object_payload = getattr(event, "object")
    return format_lemmy_post_for_discord(
        getattr(object_payload, "author_name", "remote"),
        getattr(object_payload, "title", None) or "Untitled remote post",
        normalize_text(getattr(object_payload, "body_markdown", None)),
        getattr(object_payload, "url", ""),
        actor_id=getattr(event, "actor_id", ""),
    )


def format_inbound_comment_body(event: object) -> str:
    """Render one inbound remote comment using the shared Discord-facing format."""
    object_payload = getattr(event, "object")
    return format_lemmy_comment_for_discord(
        getattr(object_payload, "author_name", "remote"),
        normalize_text(getattr(object_payload, "body_markdown", None)),
        getattr(object_payload, "url", ""),
        actor_id=getattr(event, "actor_id", ""),
    )
