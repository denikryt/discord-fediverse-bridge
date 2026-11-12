"""Edit and delete propagation helpers for shared community sync.

These helpers keep delivery filtering and AP-actor lookup out of the main
runtime while preserving the current edit/delete behavior.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from ..content_sync.edit_delete import (
    edit_discord_message,
    mark_discord_message_deleted,
)

logger = logging.getLogger(__name__)


def get_outbound_edit_deliveries(all_deliveries: list[object]) -> list[object]:
    """Return bot-owned mirror deliveries that should receive Discord edits."""
    return [delivery for delivery in all_deliveries if delivery.role == "mirror"]


def get_outbound_delete_deliveries(all_deliveries: list[object]) -> list[object]:
    """Return non-source deliveries that should be removed on source delete."""
    return [delivery for delivery in all_deliveries if delivery.role != "source"]


def get_inbound_comment_edit_deliveries(all_deliveries: list[object]) -> list[object]:
    """Return bot-owned inbound/mirror deliveries for inbound comment edits."""
    return [
        delivery
        for delivery in all_deliveries
        if delivery.role in ("inbound", "mirror")
    ]


def _activity_id(value: object) -> str | None:
    """Extract only a canonical ActivityStreams ID, never a display URL."""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict) and isinstance(value.get("id"), str):
        return value["id"]
    return None


def canonicalize_remote_delete_kind(database: object, event: object) -> object:
    """Prefer a proven Group-scoped Create kind over a Delete URI path hint.

    Call this at the trusted Python ingress before the receipt is reserved, so
    both the receipt event type and the mutation handler use the immutable
    canonical kind. Ambiguous or unknown IDs stay unchanged and are rejected
    by the later owner/kind check without cross-Group guessing.
    """
    if getattr(event, "event_type", None) not in {"post.deleted", "comment.deleted"}:
        return event
    group_id = getattr(event, "community_actor_id", None)
    object_id = getattr(event.object, "ap_id", None)
    if not group_id or not object_id:
        return event
    repository = database.discord_fanout_groups
    root = repository.get_thread_group_by_ap_object(object_id, community_actor_id=group_id)
    reply = repository.get_message_group_by_ap_object(object_id, community_actor_id=group_id)
    if (root is None) == (reply is None):
        # Either both kinds collide (unsafe) or neither kind was stored.
        return event
    kind = "post" if root is not None else "comment"
    if event.object.kind == kind:
        return event
    return event.model_copy(update={
        "event_type": f"{kind}.deleted",
        "object": event.object.model_copy(update={"kind": kind}),
    })


def inbound_mutation_denial(
    event: object, canonical: object, *, root: object | None = None,
    parent: object | None = None,
) -> str | None:
    """Authorize a signed direct mutation against immutable remote Create identity.

    Fedify is the signature boundary. A nested Update/Delete carried by a
    Group-signed Announce has no independent author signature, so its claimed
    inner actor is insufficient. No Group moderator authority has been verified
    by the current gateway contract; such mutations must fail closed.

    The caller has already selected the canonical row by (Group, object ID,
    kind). This check runs before reading deliveries or editing Discord.
    """
    group_id = getattr(event, "community_actor_id", None)
    owner = getattr(canonical, "source_owner_actor_id", None)
    if not group_id or getattr(canonical, "community_actor_id", None) != group_id:
        return "mutation Group does not match original Create"
    if not owner or not isinstance(owner, str):
        return "original Create owner is not verified"
    if not getattr(event, "actor_id", None) or event.actor_id != owner:
        return "mutation actor differs from original Create owner"
    object_owner = getattr(event.object, "attributed_to_actor_id", None)
    if object_owner and object_owner != owner:
        return "mutation object attribution differs from original owner"

    source = getattr(event, "source_activity_json", None)
    if getattr(event, "source_announce_id", None):
        return "Group distribution does not authenticate a Person mutation"
    operation = event.event_type.rsplit(".", 1)[-1]
    required_type = {"updated": "Update", "deleted": "Delete"}.get(operation)
    if (required_type is None or not isinstance(source, dict)
            or source.get("type") != required_type
            or not _activity_id(source.get("id"))
            or _activity_id(source.get("id")) != event.delivery_id
            or _activity_id(source.get("actor")) != owner):
        return "missing verified direct owner activity"
    activity_object = source.get("object")
    if _activity_id(activity_object) != event.object.ap_id:
        return "mutation object ID differs from original Create"

    # An Update's body is not evidence of authorship. Still require any
    # claimed attribution and Group to agree with the original immutable row.
    if required_type == "Update":
        # The original canonical kind cannot be rewritten into an unrelated AP
        # object type by a signed owner edit. Tombstones are Delete-only.
        if not isinstance(activity_object, dict) or activity_object.get("type") not in {
            "Note", "Article", "Page",
        }:
            return "unsupported signed Update object type"
    elif isinstance(activity_object, dict) and activity_object.get("type") not in {
        None, "Tombstone", "Note", "Article", "Page",
    }:
        return "unsupported signed Delete object type"
    if isinstance(activity_object, dict):
        attribution = activity_object.get("attributedTo")
        if attribution is not None and _activity_id(attribution) != owner:
            return "signed mutation object attributes another owner"
        audience = activity_object.get("audience")
        if audience is not None and _activity_id(audience) != group_id:
            return "mutation object addresses a different Group"
    declared_group = source.get("audience")
    if declared_group is not None and _activity_id(declared_group) != group_id:
        return "mutation activity addresses a different Group"
    if not (declared_group == group_id or (
        isinstance(activity_object, dict)
        and _activity_id(activity_object.get("audience")) == group_id
    )):
        return "mutation has no verified Group addressing"

    if root is not None:
        if (not getattr(root, "ap_object_id", None)
                or getattr(root, "id", None) != getattr(canonical, "thread_group_id", None)
                or root.community_actor_id != group_id):
            return "mutation reply root belongs to another Group"
        if parent is not None and (parent.community_actor_id != group_id
                                   or parent.thread_group_id != root.id):
            return "mutation reply parent belongs to another Group or root"
        declared_root = getattr(event.object, "post_ap_id", None)
        if declared_root is not None and declared_root != root.ap_object_id:
            return "mutation reply root differs from original Create"
        original_parent_id = getattr(canonical, "parent_message_group_id", None)
        original_parent = parent.ap_object_id if parent is not None else root.ap_object_id
        if original_parent_id is not None and parent is None:
            return "mutation original reply parent is missing"
        declared_parent = getattr(event.object, "parent_ap_id", None)
        if declared_parent is not None and declared_parent != original_parent:
            return "mutation reply parent differs from original Create"
        if isinstance(activity_object, dict):
            revision_parent = _activity_id(activity_object.get("inReplyTo"))
            if revision_parent is not None and revision_parent != original_parent:
                return "signed mutation changes original reply parent"
    elif isinstance(activity_object, dict) and activity_object.get("inReplyTo"):
        return "root mutation unexpectedly declares a reply parent"
    return None


async def resolve_actor_username(database: object, message_group: object) -> str | None:
    """Resolve the AP actor username for one source message group.

    Update/Delete activities must be authored by the same local actor that
    created the original AP object.
    """
    from ..models import MessageMapping, User

    source_message_id = getattr(message_group, "source_message_id", None)
    if source_message_id is None:
        return None

    with database.session() as session:
        mapping = session.scalar(
            select(MessageMapping).where(
                MessageMapping.discord_message_id == str(source_message_id)
            )
        )
        if mapping is None:
            return None

        user = session.scalar(select(User).where(User.actor_url == mapping.actor_url))
        return user.activitypub_username if user else None


async def propagate_inbound_post_update(
    *,
    bot: object,
    thread_deliveries: list[object],
    new_content: str,
) -> None:
    """Edit all inbound post starter messages concurrently."""

    async def edit_thread_starter(delivery: object) -> None:
        try:
            await edit_discord_message(
                bot=bot,
                discord_thread_id=delivery.discord_thread_id,
                discord_message_id=delivery.discord_starter_message_id,
                new_content=new_content,
                preserve_header=False,
            )
            logger.info(
                "Edited inbound post starter in thread %s", delivery.discord_thread_id
            )
        except Exception:
            logger.exception(
                "Failed to edit inbound post starter in thread %s",
                delivery.discord_thread_id,
            )

    await asyncio.gather(
        *[edit_thread_starter(delivery) for delivery in thread_deliveries],
        return_exceptions=True,
    )


async def propagate_inbound_post_delete(
    *,
    bot: object,
    thread_deliveries: list[object],
) -> None:
    """Mark all inbound post starter messages deleted concurrently."""

    async def mark_starter_deleted(delivery: object) -> None:
        try:
            await mark_discord_message_deleted(
                bot=bot,
                discord_thread_id=delivery.discord_thread_id,
                discord_message_id=delivery.discord_starter_message_id,
            )
            logger.info(
                "Marked inbound post starter deleted in thread %s",
                delivery.discord_thread_id,
            )
        except Exception:
            logger.exception(
                "Failed to mark inbound post starter deleted in thread %s",
                delivery.discord_thread_id,
            )

    await asyncio.gather(
        *[mark_starter_deleted(delivery) for delivery in thread_deliveries],
        return_exceptions=True,
    )


async def propagate_inbound_comment_update(
    *,
    bot: object,
    deliveries: list[object],
    new_content: str,
) -> None:
    """Edit all bot-owned inbound comment messages concurrently."""

    async def edit_message(delivery: object) -> None:
        try:
            await edit_discord_message(
                bot=bot,
                discord_thread_id=delivery.discord_thread_id,
                discord_message_id=delivery.discord_message_id,
                new_content=new_content,
                preserve_header=True,
            )
            logger.info(
                "Edited inbound comment message %s in thread %s",
                delivery.discord_message_id,
                delivery.discord_thread_id,
            )
        except Exception:
            logger.exception(
                "Failed to edit inbound comment message %s in thread %s",
                delivery.discord_message_id,
                delivery.discord_thread_id,
            )

    await asyncio.gather(
        *[edit_message(delivery) for delivery in deliveries],
        return_exceptions=True,
    )


async def propagate_inbound_comment_delete(
    *,
    bot: object,
    deliveries: list[object],
) -> None:
    """Mark all bot-owned inbound comment messages deleted concurrently."""

    async def mark_message_deleted(delivery: object) -> None:
        try:
            await mark_discord_message_deleted(
                bot=bot,
                discord_thread_id=delivery.discord_thread_id,
                discord_message_id=delivery.discord_message_id,
            )
            logger.info(
                "Marked inbound comment message %s deleted in thread %s",
                delivery.discord_message_id,
                delivery.discord_thread_id,
            )
        except Exception:
            logger.exception(
                "Failed to mark inbound comment message %s deleted in thread %s",
                delivery.discord_message_id,
                delivery.discord_thread_id,
            )

    await asyncio.gather(
        *[mark_message_deleted(delivery) for delivery in deliveries],
        return_exceptions=True,
    )
