"""Single-writer canonical community admission accompanying authored AP Create.

The stable published graph, mode-specific canonical row and verified physical
source receipt are not independent commits: a crash cannot leave an ActivityPub
Create that the community runtime can neither find nor safely replay.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ChannelSurface, ChannelCommunitySubscription, LocalCommunity, LocalSubscriber,
    CommunityThreadGroup, CommunityThreadGroupDelivery,
    LocalCommunityThread, LocalCommunityThreadSurface,
    PublishedActivityObject,
)
from ..platform_types import MessageRef


def admit_authored_post(
    session: Session, *, published: PublishedActivityObject,
    source: MessageRef, forum_channel_id: int | None,
    source_thread_id: int,
    source_starter_message_id: int,
) -> None:
    """Admit a forum post to exactly one currently owned Local/Remote domain.

    When the source is a recognized forum, source receipt insertion joins the
    AP publication writer transaction. Unclaimed/Personal Feed source remains
    outside the community domain, never assigned an arbitrary owner.
    """
    if forum_channel_id is None:
        return
    surface = session.get(ChannelSurface, published.source_channel_surface_id)
    if (surface is None or surface.kind != 'forum'
        or surface.external_channel_id != str(forum_channel_id)
        or surface.connector_id != published.source_connector_id):
        return
    content = json.dumps({
        'schemaVersion': 1, 'kind': 'post', 'title': published.title,
        'bodyMarkdown': published.body_markdown,
        'apObjectId': published.object_id,
    }, separators=(',', ':'), sort_keys=True)
    if surface.remote_subscription_id is not None:
        subscription = session.get(ChannelCommunitySubscription, surface.remote_subscription_id)
        if (subscription is None or subscription.status != 'accepted'
            or subscription.lemmy_community_actor_id != published.community_actor_url):
            raise ValueError('Remote forum ownership changed during canonical admission')
        group = CommunityThreadGroup(
            source_connector_id=surface.connector_id,
            source_channel_surface_id=surface.id,
            source_external_thread_id=source.external_container_id,
            community_actor_id=subscription.lemmy_community_actor_id,
            source_channel_id=forum_channel_id,
            source_thread_id=source_thread_id,
            source_starter_message_id=source_starter_message_id,
            ap_activity_id=published.activity_id,
            ap_object_id=published.object_id,
            current_content_json=content,
            content_revision=0,
        )
        session.add(group)
        session.flush()
        session.add(CommunityThreadGroupDelivery(
            connector_id=surface.connector_id,channel_surface_id=surface.id,
            external_thread_id=source.external_container_id,
            external_starter_message_id=source.external_message_id,
            thread_group_id=group.id,discord_channel_id=forum_channel_id,
            discord_thread_id=source_thread_id,
            discord_starter_message_id=source_starter_message_id,
            role='source',applied_revision=0,
        ))
    else:
        community = None
        role = None
        subscriber_id = None
        if surface.local_community_id is not None:
            community = session.get(LocalCommunity, surface.local_community_id)
            role='host'
            origin_kind='discord_local'
        elif surface.local_subscriber_id is not None:
            subscriber = session.get(LocalSubscriber, surface.local_subscriber_id)
            if subscriber is not None and subscriber.status == 'active':
                community = session.get(LocalCommunity, subscriber.local_community_id)
                subscriber_id = subscriber.id
                role='local_subscriber'
                origin_kind='discord_local_subscriber'
        if community is None:
            return
        if (community.status != 'active'
            or community.actor_url != published.community_actor_url):
            raise ValueError('Local forum ownership changed during canonical admission')
        thread = LocalCommunityThread(
            local_community_id=community.id,
            source_connector_id=surface.connector_id,
            source_channel_surface_id=surface.id,
            source_external_thread_id=source.external_container_id,
            source_external_starter_message_id=source.external_message_id,
            ap_activity_id=published.activity_id,
            ap_object_id=published.object_id,
            origin_kind=origin_kind,direction='discord_to_ap',
            current_content_json=content,content_revision=0,
        )
        session.add(thread)
        session.flush()
        session.add(LocalCommunityThreadSurface(
            connector_id=surface.connector_id,channel_surface_id=surface.id,
            external_thread_id=source.external_container_id,
            external_starter_message_id=source.external_message_id,
            local_community_thread_id=thread.id,
            discord_forum_channel_id=forum_channel_id,
            discord_thread_id=source_thread_id,
            discord_starter_message_id=source_starter_message_id,
            local_subscriber_id=subscriber_id,role=role,applied_revision=0,
        ))


def admit_authored_comment(
    session: Session, *, published: PublishedActivityObject, source: MessageRef,
    forum_channel_id: int | None,
) -> None:
    """Atomically persist an authored comment and its verified original receipt.

    A parent AP object must belong to the *same canonical thread and physical
    source surface*. In particular, neither an unrelated thread's AP ID nor a
    sibling mirror's message can authorize a new comment under this object.
    """
    if forum_channel_id is None or published.in_reply_to_object_id is None:
        raise ValueError('forum comment has no verified forum or AP parent')
    from ..models import (
        CommunityMessageGroup, CommunityMessageGroupDelivery,
        LocalCommunityMessage, LocalCommunityMessageSurface,
    )
    surface = session.get(ChannelSurface, published.source_channel_surface_id)
    if (surface is None or surface.kind != 'forum'
        or surface.connector_id != published.source_connector_id
        or surface.external_channel_id != str(forum_channel_id)):
        raise ValueError('forum comment is not bound to the verified source surface')
    content = json.dumps({
        'schemaVersion': 1, 'kind': 'comment', 'bodyMarkdown': published.body_markdown,
        'apObjectId': published.object_id, 'inReplyTo': published.in_reply_to_object_id,
    }, separators=(',', ':'), sort_keys=True)
    if surface.remote_subscription_id is not None:
        subscription = session.get(ChannelCommunitySubscription, surface.remote_subscription_id)
        if (subscription is None or subscription.status != 'accepted'
            or subscription.lemmy_community_actor_id != published.community_actor_url):
            raise ValueError('remote community comment ownership is not current')
        parent_surface = session.scalar(select(CommunityThreadGroupDelivery).where(
            CommunityThreadGroupDelivery.channel_surface_id == surface.id,
            CommunityThreadGroupDelivery.external_thread_id == source.external_container_id,
        ))
        if parent_surface is None:
            raise ValueError('remote comment lacks an exact physical parent thread')
        thread = session.get(CommunityThreadGroup, parent_surface.thread_group_id)
        if thread is None or thread.community_actor_id != published.community_actor_url:
            raise ValueError('remote comment belongs to another canonical community')
        parent_message_group_id = None
        if published.in_reply_to_object_id == thread.ap_object_id:
            parent_physical_id = parent_surface.external_starter_message_id
        else:
            parent = session.scalar(select(CommunityMessageGroup).where(
                CommunityMessageGroup.ap_object_id == published.in_reply_to_object_id,
                CommunityMessageGroup.thread_group_id == thread.id,
            ))
            if parent is None:
                raise ValueError('remote comment parent AP object is unrelated')
            predecessor = session.scalar(select(CommunityMessageGroupDelivery).where(
                CommunityMessageGroupDelivery.message_group_id == parent.id,
                CommunityMessageGroupDelivery.channel_surface_id == surface.id,
                CommunityMessageGroupDelivery.external_thread_id == source.external_container_id,
            ))
            if predecessor is None:
                raise ValueError('remote comment lacks an exact physical parent message')
            parent_physical_id = predecessor.external_message_id
            parent_message_group_id = parent.id
        group = CommunityMessageGroup(
            source_connector_id=surface.connector_id,
            source_channel_surface_id=surface.id,
            source_external_container_id=source.external_container_id,
            source_external_message_id=source.external_message_id,
            community_actor_id=thread.community_actor_id, thread_group_id=thread.id,
            source_channel_id=forum_channel_id,
            source_thread_id=int(source.external_container_id),
            source_message_id=int(source.external_message_id),
            ap_activity_id=published.activity_id, ap_object_id=published.object_id,
            parent_message_group_id=parent_message_group_id,
            content_revision=0, current_content_json=content,
        )
        session.add(group)
        session.flush()
        session.add(CommunityMessageGroupDelivery(
            connector_id=surface.connector_id, channel_surface_id=surface.id,
            external_thread_id=source.external_container_id,
            external_message_id=source.external_message_id,
            external_parent_message_id=parent_physical_id,
            message_group_id=group.id, discord_channel_id=forum_channel_id,
            discord_thread_id=int(source.external_container_id),
            discord_message_id=int(source.external_message_id),
            role='source', applied_revision=0,
        ))
    else:
        source_thread = session.scalar(select(LocalCommunityThreadSurface).where(
            LocalCommunityThreadSurface.connector_id == surface.connector_id,
            LocalCommunityThreadSurface.channel_surface_id == surface.id,
            LocalCommunityThreadSurface.external_thread_id == source.external_container_id,
        ))
        if source_thread is None:
            raise ValueError('local comment lacks an exact physical parent thread')
        thread = session.get(LocalCommunityThread, source_thread.local_community_thread_id)
        community = session.get(LocalCommunity, thread.local_community_id) if thread is not None else None
        if (community is None or community.status != 'active'
            or community.actor_url != published.community_actor_url):
            raise ValueError('local comment belongs to another canonical community')
        subscriber_id = source_thread.local_subscriber_id
        if subscriber_id is not None:
            subscriber = session.get(LocalSubscriber, subscriber_id)
            if (subscriber is None or subscriber.status != 'active'
                or subscriber.local_community_id != community.id):
                raise ValueError('local comment source subscriber is no longer active')
        if published.in_reply_to_object_id == thread.ap_object_id:
            parent_physical_id = source_thread.external_starter_message_id
        else:
            parent = session.scalar(select(LocalCommunityMessage).where(
                LocalCommunityMessage.ap_object_id == published.in_reply_to_object_id,
                LocalCommunityMessage.local_community_thread_id == thread.id,
            ))
            if parent is None:
                raise ValueError('local comment parent AP object is unrelated')
            predecessor = session.scalar(select(LocalCommunityMessageSurface).where(
                LocalCommunityMessageSurface.local_community_message_id == parent.id,
                LocalCommunityMessageSurface.local_community_thread_surface_id == source_thread.id,
            ))
            if predecessor is None:
                raise ValueError('local comment lacks an exact physical parent message')
            parent_physical_id = predecessor.external_message_id
        message = LocalCommunityMessage(
            local_community_thread_id=thread.id,
            source_connector_id=surface.connector_id,
            source_channel_surface_id=surface.id,
            source_external_container_id=source.external_container_id,
            source_external_message_id=source.external_message_id,
            ap_activity_id=published.activity_id,ap_object_id=published.object_id,
            parent_ap_object_id=published.in_reply_to_object_id,
            direction='discord_to_ap',content_revision=0,current_content_json=content,
        )
        session.add(message)
        session.flush()
        session.add(LocalCommunityMessageSurface(
            connector_id=surface.connector_id, channel_surface_id=surface.id,
            external_message_id=source.external_message_id,
            external_parent_message_id=parent_physical_id,
            local_community_message_id=message.id,
            local_community_thread_surface_id=source_thread.id,
            discord_forum_channel_id=forum_channel_id,
            discord_message_id=int(source.external_message_id),
            parent_discord_message_id=int(parent_physical_id),
            local_subscriber_id=subscriber_id,
            role=source_thread.role,applied_revision=0,
        ))
