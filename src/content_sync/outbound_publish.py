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


def build_discord_comment_body(
    *, author_name: str, content: str, bridge_prefix: str
) -> str:
    """Format one Discord-authored body for ActivityPub publish.

    The actual text shaping still lives in `src.formatting`; this helper keeps
    both runtimes from duplicating the same call pattern and author-name logic.
    """
    return format_discord_body_for_lemmy(author_name, content, bridge_prefix)


def build_discord_post_title(*, thread_name: str) -> str:
    """Format one Discord forum-thread title for ActivityPub publish."""
    return format_thread_title_for_discord(thread_name)


def canonical_community_create_ids(
    *, source: 'MessageRef', actor_username: str, actor_url: str, kind: str,
) -> tuple[str, str, str]:
    """Bind one physical human source to one immutable AP object and Create.

    The domain separator and length-framed tuple make connector/container/ID
    unambiguous even when the same message ID appears in two physical threads.
    Community/owner/actor are conflict-checked later, not extra ID namespaces
    that would permit duplicate AP Create on ownership reassignment.
    """
    from hashlib import sha256
    from urllib.parse import urlsplit
    from ..activitypub_identity_urls import (
        build_user_post_object_id, build_user_comment_object_id,
        build_user_create_post_activity_id, build_user_create_comment_activity_id,
    )
    from ..platform_types import MessageRef

    if not isinstance(source, MessageRef) or kind not in ('post', 'comment'):
        raise ValueError('canonical source must have typed message and kind')
    parsed = urlsplit(actor_url)
    if parsed.scheme != 'https' or not parsed.netloc or not actor_username:
        raise ValueError('canonical author origin is not an HTTPS User Actor')
    digest = sha256(b'fediverse-bridge:community-create:v1\x00')
    for part in (source.connector_key, source.external_container_id, source.external_message_id):
        data = part.encode('utf-8')
        digest.update(len(data).to_bytes(4, 'big'))
        digest.update(data)
    operation_id = digest.hexdigest()
    origin = f'{parsed.scheme}://{parsed.netloc}'
    if kind == 'post':
        return (
            operation_id,
            build_user_create_post_activity_id(origin=origin,username=actor_username,activity_id=operation_id),
            build_user_post_object_id(origin=origin,username=actor_username,object_id=operation_id),
        )
    return (
        operation_id,
        build_user_create_comment_activity_id(origin=origin,username=actor_username,activity_id=operation_id),
        build_user_comment_object_id(origin=origin,username=actor_username,object_id=operation_id),
    )
