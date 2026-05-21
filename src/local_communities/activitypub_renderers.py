"""ActivityPub renderers for local-community federation relay.

Renderers translate a durable inbound source activity into an outbound activity
that can be signed by the local community actor. The module owns only payload
shape; target selection, persistence, retry policy, and transport stay outside.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from html import escape
from uuid import uuid4

PUBLIC_COLLECTION = "https://www.w3.org/ns/activitystreams#Public"
SUPPORTED_DELIVERY_PROFILES = {
    "threadiverse_group",
    "mastodon_compat",
    "generic_activitypub",
}


def render_local_community_relay_activity(
    *,
    source_activity_json: dict,
    normalized_event: object,
    community_actor_url: str,
    community_slug: str,
    delivery_profile: str,
) -> dict:
    """Render one outbound community Announce for a target delivery profile.

    Threadiverse targets need a minimal Create(Note) projection when the source
    activity is Mastodon-shaped. Other profiles and already-threadiverse sources
    preserve the source activity exactly as before.
    """
    if delivery_profile not in SUPPORTED_DELIVERY_PROFILES:
        delivery_profile = "generic_activitypub"

    if delivery_profile == "threadiverse_group" and _needs_threadiverse_note_projection(
        source_activity_json,
        normalized_event,
    ):
        return _render_threadiverse_note_announce(
            source_activity_json=source_activity_json,
            normalized_event=normalized_event,
            community_actor_url=community_actor_url,
            community_slug=community_slug,
        )

    if delivery_profile == "threadiverse_group":
        return _render_announce(source_activity_json, community_actor_url, community_slug)
    if delivery_profile == "mastodon_compat":
        return _render_announce(source_activity_json, community_actor_url, community_slug)
    return _render_announce(source_activity_json, community_actor_url, community_slug)


def _is_create_note(activity: dict) -> bool:
    """Return whether the source activity is a Create whose object is a Note."""
    return (
        isinstance(activity, dict)
        and activity.get("type") == "Create"
        and isinstance(activity.get("object"), dict)
        and activity["object"].get("type") == "Note"
    )


def _is_threadiverse_compatible_create_note(activity: dict) -> bool:
    """Return whether a Create(Note) can be preserved for threadiverse relay.

    Lemmy accepts the existing preserve-and-announce path for its own Create
    shapes. Mastodon adds fields and compact IRIs that Lemmy's person inbox
    parser rejects, so those sources must be projected instead of copied.
    """
    if not _is_create_note(activity):
        return False

    note = activity["object"]
    if isinstance(activity.get("actor"), dict):
        return False
    if activity.get("to") == "as:Public":
        return False
    if note.get("to") == "as:Public":
        return False

    # These fields are observed on Mastodon Note payloads and are not part of
    # the minimal Lemmy-compatible Note shape used by this bridge's relay path.
    mastodon_only_note_keys = {
        "interactionPolicy",
        "contentMap",
        "context",
        "conversation",
        "likes",
        "shares",
        "replies",
    }
    return not any(key in note for key in mastodon_only_note_keys)


def _needs_threadiverse_note_projection(activity: dict, normalized_event: object) -> bool:
    """Return whether a threadiverse target needs a normalized Note projection."""
    event_type = getattr(normalized_event, "event_type", None)
    event_object = getattr(normalized_event, "object", None)
    object_kind = getattr(event_object, "kind", None)
    return (
        event_type == "comment.created"
        and object_kind == "comment"
        and _is_create_note(activity)
        and not _is_threadiverse_compatible_create_note(activity)
    )


def _render_threadiverse_note_announce(
    *,
    source_activity_json: dict,
    normalized_event: object,
    community_actor_url: str,
    community_slug: str,
) -> dict:
    """Render a Lemmy-compatible community Announce for a non-threadiverse Create(Note)."""
    event_object = getattr(normalized_event, "object")
    actor_id = getattr(normalized_event, "actor_id")
    body = getattr(event_object, "body_markdown", None) or ""
    parent_id = getattr(event_object, "parent_ap_id", None)

    note = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": getattr(event_object, "ap_id"),
        "type": "Note",
        "attributedTo": actor_id,
        "audience": community_actor_url,
        "to": [PUBLIC_COLLECTION],
        "cc": [community_actor_url, actor_id],
        "content": _markdown_text_to_html_paragraphs(body),
        "mediaType": "text/html",
        "source": {
            "content": body,
            "mediaType": "text/markdown",
        },
        "published": _isoformat_activitypub(getattr(event_object, "published_at")),
        "url": getattr(event_object, "url"),
    }
    if parent_id:
        # Parent ids come from the gateway-normalized reply chain, not from the
        # raw source object. This keeps local-parent and remote-parent replies
        # consistent after Mastodon mention stripping.
        note["inReplyTo"] = parent_id

    source_create_id = source_activity_json.get("id")
    if not isinstance(source_create_id, str) or not source_create_id:
        source_create_id = getattr(normalized_event, "delivery_id")

    create = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": source_create_id,
        "type": "Create",
        "actor": actor_id,
        "audience": community_actor_url,
        "to": [PUBLIC_COLLECTION],
        "cc": [community_actor_url, actor_id],
        "object": note,
    }
    return _render_announce(create, community_actor_url, community_slug)


def _markdown_text_to_html_paragraphs(text: str) -> str:
    """Render normalized bridge text as conservative ActivityPub HTML."""
    # Gateway normalization already produced plain markdown-ish text. Escape it
    # before putting it into ActivityPub HTML so remote HTML cannot leak back
    # into Lemmy via relay projection.
    escaped = escape(text.strip())
    return f"<p>{escaped.replace(chr(10), '<br />')}</p>"


def _isoformat_activitypub(value: object) -> str:
    """Return an ActivityPub timestamp string from a normalized event value."""
    if isinstance(value, str):
        return value
    if hasattr(value, "isoformat"):
        rendered = value.isoformat()
        return rendered.replace("+00:00", "Z")
    return str(value)


def _render_announce(source_activity_json: dict, community_actor_url: str, community_slug: str) -> dict:
    """Build a community-owned Announce without mutating the source activity."""
    # Deep-copying protects the persisted source JSON from accidental renderer
    # mutation while preserving the original actor and object attribution.
    source_activity = deepcopy(source_activity_json)
    announce_id = (
        f"{community_actor_url.rstrip('/')}/activities/announce/"
        f"{datetime.now(timezone.utc).timestamp():.6f}-{uuid4().hex}"
    )
    followers_url = f"{community_actor_url.rstrip('/')}/followers"
    return {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": announce_id,
        "type": "Announce",
        "actor": community_actor_url,
        "to": [PUBLIC_COLLECTION],
        "cc": [followers_url],
        "object": source_activity,
    }
