"""Wire scenario Runtime dependencies to real repositories and fake transport edges."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

from sqlalchemy import select
from src.content_sync.platform_mutations import PlatformPhysicalMutationCoordinator
from src.fedify_gateway_client import CanonicalMutationResult
from src.formatting import format_lemmy_post_for_discord, format_lemmy_comment_for_discord
from src.models import (ChannelSurface, CommunityThreadGroup, CommunityMessageGroup,
                       LocalCommunityThread, LocalCommunityMessage)
from src.platform_types import ContentDocument
from src.user_actor_delivery import UserActorDeliveryService
from src.community_sync.runtime import CommunityRuntime
from src.local_communities.runtime import LocalCommunityRuntime
from support.community_events import ScenarioDiscord


def wire(namespace):
    """Fill explicitly requested domain Runtime graphs; retain supplied dependencies."""
    local = getattr(namespace, 'local_community_runtime', None)
    remote = getattr(namespace, 'community_runtime', None)
    owner = local if isinstance(local, LocalCommunityRuntime) else remote
    if not isinstance(owner, (LocalCommunityRuntime, CommunityRuntime)):
        return namespace
    db = owner.database
    namespace.database = db
    if getattr(owner, 'bot', None) is None:
        owner.bot = getattr(namespace, 'bot', None)
    if not hasattr(namespace, 'discord_adapter'):
        adapter = getattr(owner, 'discord_adapter', None) or ScenarioDiscord(owner)
        namespace.discord_adapter = adapter
        if hasattr(owner, 'bind_discord_adapter') and getattr(owner, 'discord_adapter', None) is None:
            owner.bind_discord_adapter(adapter)
    gateway = getattr(namespace, 'fedify_gateway', None) or getattr(owner, 'fedify_gateway', None)
    if gateway is None and hasattr(owner, 'content_publish_service'):
        gateway = getattr(owner.content_publish_service, 'fedify_gateway', None)
    policy = getattr(namespace, 'bridge_policy_service', None)
    if gateway is not None and not hasattr(namespace, 'user_actor_delivery_service'):
        for name, kind in [('update_content', 'Update'), ('delete_content', 'Delete')]:
            boundary = getattr(gateway, name, None)
            if (isinstance(boundary, AsyncMock) and boundary.side_effect is None
                and isinstance(boundary.return_value, AsyncMock)):
                async def render(request, kind=kind):
                    """Return immutable mutation JSON, leaving canonical commit real."""
                    user = db.users.get_user_by_activitypub_username(request.actor_username)
                    obj = (request.ap_object_id if kind == 'Delete' else
                           {'id': request.ap_object_id, 'content': request.body_markdown})
                    return CanonicalMutationResult(request.activity_id, request.ap_object_id,
                        {'id': request.activity_id, 'type': kind, 'actor': user.actor_url, 'object': obj})
                boundary.side_effect = render
        namespace.user_actor_delivery_service = UserActorDeliveryService(db, gateway, policy)
        namespace.user_actor_delivery_worker = SimpleNamespace(wake=lambda: None)
    if not hasattr(namespace, 'physical_mutation_coordinator'):
        def allowed(connector, surface_id):
            """Recheck actual surface ownership metadata through current policy."""
            with db.session() as session:
                surface = session.get(ChannelSurface, surface_id)
                return bool(surface and surface.external_guild_id
                            and (policy is None or policy.is_discord_guild_allowed(surface.external_guild_id)))
        def content(domain, kind, snapshot):
            """Use the same shared presentation functions as the production composition."""
            body = snapshot['bodyMarkdown']; author = snapshot.get('authorName') or 'remote'
            if snapshot.get('sourceKind') == 'discord_human':
                return ContentDocument(f'`{author}`\n\n{body}' if body else f'`{author}`')
            url = snapshot.get('url') or snapshot['apObjectId']; actor = snapshot.get('authorActorId') or ''
            return ContentDocument(format_lemmy_post_for_discord(author,
                snapshot.get('title') or 'Untitled remote post', body, url, actor)
                if kind == 'post' else format_lemmy_comment_for_discord(author, body, url, actor))
        namespace.physical_mutation_coordinator = PlatformPhysicalMutationCoordinator(db,
            SimpleNamespace(get=lambda key: namespace.discord_adapter), target_policy=allowed,
            content_renderer=content, tombstone_renderer=lambda domain, kind: ContentDocument('*deleted by creator*'))
    return namespace


def retain_original_author(db, *, domain, kind, object_id, actor_id, author_name):
    """Seed the known original authenticated Create, independently of the next event."""
    model = {('remote','post'):CommunityThreadGroup, ('remote','comment'):CommunityMessageGroup,
             ('local','post'):LocalCommunityThread, ('local','comment'):LocalCommunityMessage}[domain,kind]
    with db.immediate_session() as session:
        row = session.scalar(select(model).where(model.ap_object_id==object_id))
        assert row is not None and row.content_revision == 0
        row.current_content_json = json.dumps({'schemaVersion':1, 'kind':kind,
            'apObjectId':object_id, 'authorActorId':actor_id, 'authorName':author_name,
            'bodyMarkdown':'original body'})


def attach(namespace, owner):
    """Supply one explicit domain owner before wiring a direct-handler test call."""
    if isinstance(owner, LocalCommunityRuntime):
        namespace.local_community_runtime = owner
    else:
        namespace.community_runtime = owner
    if not hasattr(namespace, 'bridge_policy_service'):
        namespace.bridge_policy_service = (owner.bridge_policy_service if isinstance(owner, LocalCommunityRuntime)
            else owner.content_publish_service.bridge_policy_service)
    return wire(namespace)
