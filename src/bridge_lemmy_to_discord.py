from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiohttp
import discord

from .activitypub_models import ActivityPubEvent, ActivityPubObject
from .db import Database
from .formatting import format_lemmy_comment_for_discord, format_lemmy_post_for_discord, format_thread_title_for_discord, normalize_text

logger = logging.getLogger(__name__)


async def _fetch_ap_object(url: str) -> dict:
    """Fetch one ActivityPub object by URL and return its parsed JSON dict.

    Sends GET with Accept: application/activity+json. Raises on non-2xx
    HTTP status codes or non-JSON response bodies. Callers treat any
    exception as a fetch failure and fall back to the deferred path.
    """
    headers = {"Accept": "application/activity+json"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status} fetching AP object {url}")
            return await resp.json(content_type=None)


def _build_post_event_from_ap_doc(
    doc: dict,
    community_actor_id: str,
    delivery_id: str,
) -> ActivityPubEvent:
    """Construct a synthetic post.created ActivityPubEvent from a raw AP Page document.

    Used during comment backfill when the parent post was never delivered: we fetch
    the post document directly from the remote instance and synthesize an event so
    the normal handle_inbound_post path can create the Discord thread.

    Field mapping from Lemmy AP Page documents:
    - title:         doc["name"]
    - body_markdown: doc["source"]["content"] if present, else None
    - url:           doc["attachment"][0]["href"] if non-empty and differs from ap_id,
                     else doc["id"] (post URL on Lemmy used as fallback for text-only posts)
    - author_name:   last path segment of doc["attributedTo"]
    - ap_id:         doc["id"]
    - lemmy_id:      numeric suffix of doc["id"]; 0 on parse failure
    - published_at:  doc["published"]
    """
    ap_id: str = doc["id"]

    # Extract the external article URL from the first Link attachment when present.
    # Lemmy link-posts store the external URL in attachment[0].href, not in any
    # top-level url field. Fall back to the Lemmy post URL (ap_id) for text-only posts.
    url = ap_id
    attachments = doc.get("attachment") or []
    for att in attachments:
        href = att.get("href") if isinstance(att, dict) else None
        if href and href != ap_id:
            url = href
            break

    # Prefer markdown source body; rendered HTML (content) is not used.
    source = doc.get("source")
    body_markdown: str | None = None
    if isinstance(source, dict):
        body_markdown = source.get("content")

    # author_name is the last path segment of attributedTo URL.
    attributed_to: str = doc.get("attributedTo", "")
    author_name = attributed_to.rstrip("/").rsplit("/", 1)[-1] or "unknown"

    # Parse the numeric Lemmy post ID from the AP object URL suffix.
    try:
        lemmy_id = int(ap_id.rstrip("/").rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        lemmy_id = 0

    published_at_raw: str = doc.get("published", datetime.now(timezone.utc).isoformat())

    return ActivityPubEvent.model_validate(
        {
            "event_type": "post.created",
            "delivery_id": delivery_id,
            "occurred_at": published_at_raw,
            "community_actor_id": community_actor_id,
            "actor_id": attributed_to or community_actor_id,
            "object": {
                "ap_id": ap_id,
                "kind": "post",
                "lemmy_id": lemmy_id,
                "post_ap_id": None,
                "post_lemmy_id": None,
                "parent_ap_id": None,
                "title": doc.get("name") or "Untitled Post",
                "body_markdown": body_markdown,
                "url": url,
                "published_at": published_at_raw,
                "author_name": author_name,
            },
        }
    )


async def _create_inbound_discord_thread(
    *,
    forum_channel: discord.ForumChannel,
    event: ActivityPubEvent,
) -> tuple[int, int]:
    """Create one Discord forum-thread for an inbound AP post.

    Returns (thread_id, starter_message_id). No DB writes — the caller
    (CommunityRuntime.handle_inbound_post) persists the delivery rows.
    """
    post = event.object
    title = format_thread_title_for_discord(post.title or "Untitled Lemmy Post")
    body = format_lemmy_post_for_discord(
        post.author_name,
        post.title or "Untitled Lemmy Post",
        normalize_text(post.body_markdown),
        post.url,
        actor_id=event.actor_id,
    )

    result = await forum_channel.create_thread(name=title, content=body)
    thread = getattr(result, "thread", None)
    message = getattr(result, "message", None)
    if thread is None and isinstance(result, tuple):
        thread = result[0]
    if message is None and isinstance(result, tuple):
        message = result[1]
    if thread is None:
        raise RuntimeError("Discord create_thread did not return a thread object")
    if message is None:
        # Some discord.py variants do not return the starter message, so recover
        # it immediately before returning — caller needs the starter_message_id.
        try:
            message = await thread.fetch_message(thread.id)
        except discord.HTTPException as exc:
            raise RuntimeError("Discord create_thread did not return a starter message") from exc

    logger.info("Created Discord forum thread %s from ActivityPub post %s", thread.id, post.ap_id)
    return thread.id, message.id


async def _send_inbound_comment(
    *,
    thread: discord.Thread,
    event: ActivityPubEvent,
    reference: discord.MessageReference | None,
) -> discord.Message:
    """Send one Discord message for an inbound AP comment.

    Returns the sent Discord message. No DB writes — the caller
    (CommunityRuntime.handle_inbound_comment) persists the delivery row.
    The reference is already resolved by the caller via _resolve_inbound_reference.
    """
    comment = event.object
    body = format_lemmy_comment_for_discord(
        comment.author_name,
        normalize_text(comment.body_markdown),
        comment.url,
        actor_id=event.actor_id,
    )
    message = await thread.send(body, reference=reference)
    logger.info("Created Discord message %s from ActivityPub comment %s", message.id, comment.ap_id)
    return message
