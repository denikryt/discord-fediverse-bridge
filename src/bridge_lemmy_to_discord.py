from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiohttp
import discord

from .activitypub_models import ActivityPubEvent, ActivityPubObject
from .federated_media import (DownloadedImage, can_attach_files, discord_files,
    download_images, image_presentation, media_source_from_note)
from .formatting import (
    render_remote_community_root,
    render_remote_community_comment,
    format_thread_title_for_discord,
    normalize_text,
)

logger = logging.getLogger(__name__)


class DiscordContentPreflightError(ValueError):
    """Local rendering rejected an outbound Create before Discord REST began."""



async def _fetch_ap_object(url: str) -> dict:
    """Fetch one ActivityPub object by URL and return its parsed JSON dict.

    Sends GET with Accept: application/activity+json. Raises on non-2xx
    HTTP status codes or non-JSON response bodies. Callers treat any
    exception as a fetch failure and fall back to the deferred path.
    """
    headers = {"Accept": "application/activity+json"}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
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
                "media_source": media_source_from_note(doc),
                "url": url,
                "published_at": published_at_raw,
                "author_name": author_name,
            },
        }
    )


def community_image_presentation(post: object) -> tuple[str, tuple[str, ...]]:
    """Resolve current Community body/links from its immutable revision snapshot."""
    return image_presentation(post.body_markdown, getattr(post, 'media_source', None),
        post.ap_id, external_url=getattr(post, 'url', None) if getattr(post, 'kind', None) == 'post' else None)


async def prepare_community_create(repository, *, event, reservation, kind, group_id,
                                   channel_id, destination):
    """Download before the final claim fence; refresh if a live Update races.

    Repeated revisions cannot prevent text delivery: after two refreshes the
    final current view is sent without files, never with stale attachments.
    """
    attempt = reservation.attempt_id
    for index in range(3):
        obj = ActivityPubObject.model_validate(reservation.display_object_json)
        _, urls = community_image_presentation(obj)
        # The last pass is a bounded text-only fallback under continual edits.
        images = await download_images(urls) if index < 2 and can_attach_files(destination) else ()
        confirmed = repository.confirm_remote_target_pre_send(kind=kind, group_id=group_id,
            discord_channel_id=channel_id, attempt_id=attempt,
            prepared_revision=reservation.view_revision if index < 2 else None)
        if confirmed is None:
            return None
        if confirmed.state == 'refresh':
            reservation = confirmed
            continue
        current = ActivityPubObject.model_validate(confirmed.display_object_json)
        return event.model_copy(update={'object': current}), images
    return None


async def _create_inbound_discord_thread(
    *,
    forum_channel: discord.ForumChannel,
    event: ActivityPubEvent,
    images: tuple[DownloadedImage, ...] = (),
) -> tuple[int, int]:
    """Create one Discord forum-thread for an inbound AP post.

    Returns (thread_id, starter_message_id). No DB writes — the caller
    (CommunityRuntime.handle_inbound_post) persists the delivery rows.
    """
    post = event.object
    title = format_thread_title_for_discord(post.title or "Untitled Lemmy Post")
    text, _ = community_image_presentation(post)
    try:
        body = render_remote_community_root(
            actor_name=post.author_name, actor_id=event.actor_id,
            object_id=post.ap_id, title=post.title,
            body=text, external_url=None,
        )
    except ValueError as exc:
        # Local layout validation is provably before create_thread. A timeout
        # on the actual Discord call is handled separately by the runtime.
        raise DiscordContentPreflightError(str(exc)) from exc

    # Normal inbound runtime supplies files before its durable POST fence.
    # Do not add network work to legacy callers without that fence.
    with discord_files(images if can_attach_files(forum_channel) else ()) as files:
        options = {'files': files} if files else {}
        result = await forum_channel.create_thread(name=title, content=body,
            suppress_embeds=True, **options)
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
            raise RuntimeError(
                "Discord create_thread did not return a starter message"
            ) from exc

    logger.info(
        "Created Discord forum thread %s from ActivityPub post %s",
        thread.id,
        post.ap_id,
    )
    return thread.id, message.id


async def _send_inbound_comment(
    *,
    thread: discord.Thread,
    event: ActivityPubEvent,
    reference: discord.MessageReference | None,
    images: tuple[DownloadedImage, ...] = (),
) -> discord.Message:
    """Send one Discord message for an inbound AP comment.

    Returns the sent Discord message. No DB writes — the caller
    (CommunityRuntime.handle_inbound_comment) persists the delivery row.
    The reference is already resolved by the caller via _resolve_inbound_reference.
    """
    comment = event.object
    text, _ = community_image_presentation(comment)
    try:
        body = render_remote_community_comment(
            actor_name=comment.author_name, actor_id=event.actor_id,
            object_id=comment.ap_id, body=text,
        )
    except ValueError as exc:
        raise DiscordContentPreflightError(str(exc)) from exc
    with discord_files(images if can_attach_files(thread) else ()) as files:
        options = {'files': files} if files else {}
        message = await thread.send(body, reference=reference, suppress_embeds=True, **options)
    logger.info(
        "Created Discord message %s from ActivityPub comment %s",
        message.id,
        comment.ap_id,
    )
    return message
