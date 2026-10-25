"""Scenario boundary observations for the single typed Community Create pipeline.

Only gateway rendering and physical Discord I/O are faked. Domain admission,
canonical publication, ownership, durable intents and receipts remain real.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.platform_types import (
    AuthorRef, ChannelRef, ContentDocument, CreatedMessage, CreatedThread,
    MessageCreated, MessageRef, MessageView, ThreadCreated, ThreadRef,
)
from src.fedify_gateway_client import RenderCommunityResult
from support.fake_platform_adapter import FakeDiscordPort

OBSERVED_AT = datetime(2026, 10, 20, tzinfo=timezone.utc)


def render_create(request, actor_url):
    """Render the caller's immutable IDs and source content without delivery."""
    obj = dict(id=request.object_id, type='Page' if request.kind == 'post' else 'Note',
               attributedTo=actor_url, published=request.published_at,
               source={'content': request.body_markdown, 'mediaType': 'text/markdown'},
               audience=request.community_actor_url)
    if request.kind == 'post':
        obj['name'] = request.title
    else:
        obj['inReplyTo'] = request.in_reply_to_object_id
    activity = dict(id=request.activity_id, type='Create', actor=actor_url,
                    to=['https://www.w3.org/ns/activitystreams#Public', request.community_actor_url],
                    cc=[f'{actor_url}/followers'], object=obj)
    return RenderCommunityResult(request.activity_id, request.object_id,
                                 request.community_actor_url, request.published_at, activity)


def configure_renderer(gateway, database):
    """Attach a render-only boundary to an existing scenario gateway."""
    async def render(request):
        """Resolve the real registered author for the requested username."""
        user = database.users.get_user_by_activitypub_username(request.actor_username)
        return render_create(request, user.actor_url)
    gateway.render_community = AsyncMock(side_effect=render)


class ScenarioDiscord(FakeDiscordPort):
    """Adapt explicit SDK edge fakes into exact typed physical receipts."""

    def __init__(self, runtime, notice=None):
        """Borrow the scenario's configured transport without routing decisions."""
        self.runtime = runtime
        self.notice = notice

    async def create_forum_thread(self, forum, content, *, title, presentation, attempt):
        """Return the exact IDs supplied by the scenario's physical forum fake."""
        channel = await self.runtime.bot.fetch_forum_channel(int(forum.external_channel_id))
        result = await channel.create_thread(name=title, content=content.body)
        thread, starter = result if isinstance(result, tuple) else (result.thread, result.message)
        target = ThreadRef(forum, str(thread.id))
        return CreatedThread(target, MessageRef('discord-primary', str(thread.id), str(starter.id)), 'bot')

    async def send_message(self, destination, content, *, reply_to, presentation, attempt):
        """Keep physical parent IDs visible to existing SDK boundary assertions."""
        if self.notice is not None and attempt.key.startswith('community-'):
            await self.notice(content.body)
            return CreatedMessage(MessageRef('discord-primary', destination.external_thread_id, '999999'), 'bot')
        thread = await self.runtime.bot.get_thread_by_id(int(destination.external_thread_id))
        from discord import MessageReference
        reference = (MessageReference(message_id=int(reply_to.external_message_id),
            channel_id=int(reply_to.external_container_id), fail_if_not_exists=False) if reply_to else None)
        result = await thread.send(content.body, reference=reference)
        return CreatedMessage(MessageRef('discord-primary', destination.external_thread_id, str(result.id)), 'bot')


    async def fetch_message(self, ref):
        """Resolve the exact fixture message and positively identify its bot copy."""
        thread = await self.runtime.bot.get_thread_by_id(int(ref.external_container_id))
        physical = await thread.fetch_message(int(ref.external_message_id))
        body = getattr(physical, 'content', '')
        return MessageView(ref, AuthorRef(ref.connector_key, 'bridge-bot', 'bot', 'Bridge', True),
                           ContentDocument(body if isinstance(body, str) else ''))

    async def edit_message(self, ref, content):
        """Expose one typed physical edit at the scenario's SDK fake boundary."""
        thread = await self.runtime.bot.get_thread_by_id(int(ref.external_container_id))
        physical = await thread.fetch_message(int(ref.external_message_id))
        await physical.edit(content=content.body)

    async def delete_message(self, ref):
        """Delete only the requested physical message in source-mutation scenarios."""
        thread = await self.runtime.bot.get_thread_by_id(int(ref.external_container_id))
        physical = await thread.fetch_message(int(ref.external_message_id))
        await physical.delete()


def source_author(author):
    """Represent only the observed human identity, never a supplied AP actor."""
    return AuthorRef('discord-primary', str(author.id), 'human', author.display_name)


def observe_thread(*, thread, starter_message):
    """Build one authenticated source observation with an original timestamp."""
    forum = ChannelRef('discord-primary', str(thread.parent_id), 'forum', '10')
    ref = ThreadRef(forum, str(thread.id))
    opening = MessageRef('discord-primary', str(thread.id), str(starter_message.id))
    event = ThreadCreated('discord-primary', ref, thread.name, starter=opening, external_guild_id='10')
    view = MessageView(opening, source_author(starter_message.author),
                       ContentDocument(starter_message.content), created_at=OBSERVED_AT)
    return event, view


async def create_thread(runtime, *, thread, starter_message):
    """Route a source observation through the production typed Create handler."""
    event, view = observe_thread(thread=thread, starter_message=starter_message)
    return await runtime.handle_normalized_thread_create(
        event, view, adapter=ScenarioDiscord(runtime, starter_message.reply))


def observe_message(message):
    """Build a reply observation scoped to its immediate physical container."""
    forum = ChannelRef('discord-primary', str(message.channel.parent_id), 'forum', '10')
    thread = ThreadRef(forum, str(message.channel.id))
    parent_id = getattr(getattr(message, 'reference', None), 'message_id', None)
    parent = MessageRef('discord-primary', str(message.channel.id), str(parent_id)) if parent_id else None
    event = MessageCreated('discord-primary',
        MessageRef('discord-primary', str(message.channel.id), str(message.id)),
        source_author(message.author), ContentDocument(message.content),
        parent_channel=forum, thread_context=thread, external_guild_id='10',
        reply_to=parent, created_at=OBSERVED_AT)
    return event


async def create_message(runtime, message):
    """Route a reply through the production typed Create handler."""
    return await runtime.handle_normalized_message_create(
        observe_message(message), adapter=ScenarioDiscord(runtime, getattr(message, 'reply', None)))


def source_ids(database, *, thread=200, message=300, kind='post', author='123'):
    """Compute the public deterministic identity for an explicit observed source."""
    from src.content_sync.outbound_publish import canonical_community_create_ids
    user = database.users.get_user_by_discord_user_id(author)
    return canonical_community_create_ids(
        source=MessageRef('discord-primary', str(thread), str(message)),
        actor_username=user.activitypub_username, actor_url=user.actor_url, kind=kind,
    )


def physical_forum(database, receipt):
    """Read the forum from typed receipt metadata rather than NULL projections."""
    from src.models import ChannelSurface
    with database.session() as session:
        return int(session.get(ChannelSurface, receipt.channel_surface_id).external_channel_id)


def message_copy(database, message_id):
    """Find a test's uniquely numbered receipt under the actual Discord connector."""
    from sqlalchemy import select
    from src.models import LocalCommunityMessageSurface, PlatformConnector
    with database.session() as session:
        return session.scalar(select(LocalCommunityMessageSurface).join(
            PlatformConnector, PlatformConnector.id == LocalCommunityMessageSurface.connector_id,
        ).where(PlatformConnector.connector_key == 'discord-primary',
                LocalCommunityMessageSurface.external_message_id == str(message_id)))


async def admit_thread(runtime, *, thread, starter_message, community):
    """Model a crash after canonical commit and before any recipient dispatch."""
    event, view = observe_thread(thread=thread, starter_message=starter_message)
    result = await runtime.content_publish_service.publish_normalized_post_to_community(
        event=event, starter=view, community_actor_url=community.actor_url,
        publish_call=runtime.fedify_gateway.publish_local_community_content,
        target_inbox_urls=[], notice_adapter=ScenarioDiscord(runtime),
    )
    assert result.status == 'published'
    return runtime.database.local_community_content.get_local_community_thread_by_ap_object_id(result.object_id)


def mutation_runtime(runtime):
    """Wire real durable author mutations to deterministic gateway I/O boundaries."""
    from src.fedify_gateway_client import CanonicalMutationResult
    from src.user_actor_delivery import UserActorDeliveryService

    async def update(request):
        """Render one exact authored Update for the durable mutation request."""
        actor = runtime.database.users.get_user_by_activitypub_username(request.actor_username).actor_url
        return CanonicalMutationResult(request.activity_id, request.ap_object_id, {
            'id': request.activity_id, 'type': 'Update', 'actor': actor,
            'object': {'id': request.ap_object_id, 'type': 'Page',
                       'attributedTo': actor, 'content': request.body_markdown},
        })

    async def delete(request):
        """Render a Delete retaining the original author and canonical object."""
        actor = runtime.database.users.get_user_by_activitypub_username(request.actor_username).actor_url
        return CanonicalMutationResult(request.activity_id, request.ap_object_id, {
            'id': request.activity_id, 'type': 'Delete', 'actor': actor, 'object': request.ap_object_id,
        })

    gateway = runtime.fedify_gateway
    gateway.update_content = AsyncMock(side_effect=update)
    gateway.delete_content = AsyncMock(side_effect=delete)
    policy = runtime.bridge_policy_service
    return SimpleNamespace(
        user_actor_delivery_service=UserActorDeliveryService(runtime.database, gateway, policy),
        bridge_policy_service=policy, user_actor_delivery_worker=SimpleNamespace(wake=lambda: None),
    )
