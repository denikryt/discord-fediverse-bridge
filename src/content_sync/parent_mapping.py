"""Connector-scoped physical reply-parent resolution for both community domains.

The canonical AP parent identifies one logical post or reply. Each recipient
forum instead owns a distinct physical MessageRef; an absent known parent is
an explicit dependency, never permission to flatten onto a different root.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ChannelSurface, CommunityMessageGroup, CommunityMessageGroupDelivery,
    CommunityThreadGroupDelivery, LocalCommunityMessage,
    LocalCommunityMessageSurface, LocalCommunityThread,
    LocalCommunityThreadSurface, PlatformConnector,
)
from ..platform_types import MessageRef, ThreadRef


@dataclass(frozen=True, slots=True)
class MissingParent:
    """A known canonical AP reply parent lacks its exact destination receipt."""

    canonical_parent_id: int
    reason: str


def resolve_target_parent(
    session: Session,
    *,
    domain: str,
    canonical_thread_id: int,
    target: ThreadRef,
    target_surface_id: int,
    canonical_parent_message_id: int | None,
) -> MessageRef | MissingParent:
    """Find the precise target starter or nested parent without SDK IDs.

    Identity is verified against the registered `(connector, forum surface)`,
    canonical thread and exact physical thread container. Unknown or foreign
    canonical parents raise rather than permitting a flat fallback. Return
    ``MissingParent`` only when the *known* parent lacks a physical receipt.
    """
    if not isinstance(target, ThreadRef) or target.channel.kind != 'forum':
        raise ValueError('target parent requires a typed forum thread')
    connector = session.scalar(select(PlatformConnector).where(
        PlatformConnector.connector_key == target.connector_key))
    surface = session.get(ChannelSurface, target_surface_id)
    if (connector is None or surface is None
        or surface.connector_id != connector.id
        or surface.kind != 'forum'
        or surface.external_channel_id != target.channel.external_channel_id
        or surface.external_guild_id != target.channel.external_guild_id):
        raise ValueError('target parent forum has mismatched connector/surface')

    if domain == 'local':
        # The thread-surface is a proven physical root in the target forum.
        thread_surface = session.scalar(select(LocalCommunityThreadSurface).where(
            LocalCommunityThreadSurface.local_community_thread_id == canonical_thread_id,
            LocalCommunityThreadSurface.channel_surface_id == target_surface_id))
        if (thread_surface is None or thread_surface.connector_id != connector.id
            or thread_surface.external_thread_id != target.external_thread_id):
            raise ValueError('target physical thread is not a verified canonical copy')
        if canonical_parent_message_id is None:
            message_id = thread_surface.external_starter_message_id
        else:
            parent = session.get(LocalCommunityMessage, canonical_parent_message_id)
            if (parent is None or parent.local_community_thread_id != canonical_thread_id
                or parent.deleted_at is not None):
                raise ValueError('known AP reply parent is foreign or deleted')
            copy = session.scalar(select(LocalCommunityMessageSurface).where(
                LocalCommunityMessageSurface.local_community_message_id == parent.id,
                LocalCommunityMessageSurface.local_community_thread_surface_id == thread_surface.id))
            if copy is None:
                return MissingParent(parent.id, 'target_comment_receipt_missing')
            if (copy.connector_id != connector.id or copy.channel_surface_id != target_surface_id):
                raise ValueError('nested reply copy has mismatched physical ownership')
            message_id = copy.external_message_id
    elif domain == 'remote':
        thread_surface = session.scalar(select(CommunityThreadGroupDelivery).where(
            CommunityThreadGroupDelivery.thread_group_id == canonical_thread_id,
            CommunityThreadGroupDelivery.channel_surface_id == target_surface_id))
        if (thread_surface is None or thread_surface.connector_id != connector.id
            or thread_surface.external_thread_id != target.external_thread_id):
            raise ValueError('target physical thread is not a verified canonical copy')
        if canonical_parent_message_id is None:
            message_id = thread_surface.external_starter_message_id
        else:
            parent = session.get(CommunityMessageGroup, canonical_parent_message_id)
            if (parent is None or parent.thread_group_id != canonical_thread_id
                or parent.deleted_at is not None):
                raise ValueError('known AP reply parent is foreign or deleted')
            copy = session.scalar(select(CommunityMessageGroupDelivery).where(
                CommunityMessageGroupDelivery.message_group_id == parent.id,
                CommunityMessageGroupDelivery.channel_surface_id == target_surface_id))
            if copy is None:
                return MissingParent(parent.id, 'target_comment_receipt_missing')
            if (copy.connector_id != connector.id
                or copy.external_thread_id != target.external_thread_id):
                raise ValueError('nested reply physical parent is in another container')
            message_id = copy.external_message_id
    else:
        raise ValueError('unknown canonical community domain')
    if not message_id:
        raise ValueError('physical parent lacks a verified message id')
    return MessageRef(target.connector_key, target.external_thread_id, message_id)
