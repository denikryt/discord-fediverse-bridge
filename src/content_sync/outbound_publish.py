"""Shared outbound content preparation helpers.

These helpers hold the reusable pre-publish steps that both bridge modes need:
registered-user resolution, unregistered-user feedback, and Discord -> AP body
and title formatting.
"""

from __future__ import annotations

from ..formatting import format_discord_body_for_lemmy, format_thread_title_for_discord


async def resolve_registered_user(
    *,
    database: object,
    author: object,
    reply_target: object,
    unregistered_reply: str,
) -> object | None:
    """Resolve the registered local user for one Discord author.

    Both bridge modes require an existing local ActivityPub identity before a
    Discord-authored post or comment can be federated. The caller supplies the
    reply boundary so the same rejection contract is preserved everywhere.
    """
    user = database.users.get_user_by_discord_user_id(str(getattr(author, "id")))
    if user is not None:
        return user

    await getattr(reply_target, "reply")(unregistered_reply)
    return None


def build_discord_comment_body(*, author_name: str, content: str, bridge_prefix: str) -> str:
    """Format one Discord-authored body for ActivityPub publish.

    The actual text shaping still lives in `src.formatting`; this helper keeps
    both runtimes from duplicating the same call pattern and author-name logic.
    """
    return format_discord_body_for_lemmy(author_name, content, bridge_prefix)


def build_discord_post_title(*, thread_name: str) -> str:
    """Format one Discord forum-thread title for ActivityPub publish."""
    return format_thread_title_for_discord(thread_name)
