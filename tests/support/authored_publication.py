"""Prepare an inherited authored Community source with independently verifiable identity.

Unlike a bare PublishedActivityObject, this state includes its accepted forum,
exact MessageRef, generic source mapping and canonical domain row/receipt.
No outbound source or inbox permission is fabricated by a caller-provided list.
"""
from sqlalchemy import select
from src.models import ChannelSurface, LocalCommunity, PublishedActivityObject
from src.platform_types import MessageRef
from src.content_sync.canonical_admission import admit_authored_post


def publication(database, **fields):
    """Initialize a valid persisted post source without replaying its historical Create."""
    container = fields['discord_channel_id']
    with database.session() as session:
        local = session.scalar(select(LocalCommunity).where(LocalCommunity.actor_url == fields['community_actor_url']))
        placement = session.scalar(select(ChannelSurface).where(ChannelSurface.local_community_id == local.id)) if local else None
    forum = int(placement.external_channel_id) if placement else container
    if local is None and database.remote_subscriptions.get_subscription_by_channel(forum) is None:
        # Only explicit inbox evidence grants a direct recipient. These inherited
        # follower-focused cases have an accepted surface, but no retained inbox.
        database.remote_subscriptions.create_subscription(discord_channel_id=forum,
            discord_guild_id=1, lemmy_community_actor_id=fields['community_actor_url'],
            lemmy_community_name='group', lemmy_community_id=1,
            follow_activity_id='https://bridge.example/actors/services/bridge/activities/follow/source',
            status='accepted')
    fields['discord_channel_id'] = forum
    published = database.activitypub_objects.create_published_activity_object(**fields)
    source = MessageRef('discord-primary', str(container), str(fields['discord_message_id']))
    database.message_mappings.create_message_mapping(
        source_platform='discord', source_id=source.external_message_id,
        activity_id=published.activity_id, object_id=published.object_id,
        actor_url=published.actor_url, community_actor_url=published.community_actor_url,
        discord_channel_id=forum, discord_message_id=fields['discord_message_id'], source_message=source)
    with database.immediate_session() as session:
        row = session.get(PublishedActivityObject, published.id)
        row.source_external_container_id = source.external_container_id
        admit_authored_post(session, published=row, source=source, forum_channel_id=forum,
            source_thread_id=container, source_starter_message_id=fields['discord_message_id'])
    return published
