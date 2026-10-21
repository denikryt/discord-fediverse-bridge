"""Authenticated read API exposing Python-owned persistence to Fedify Gateway."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, model_validator

from .actor_key_service import ActorKeyService
from .internal_auth import validate_internal_bearer
from .runtime import Runtime
from .models import ProfileMediaAsset


class ActorUrlRequest(BaseModel):
    """Carry a canonical actor URL for internal actor resolution."""

    actor_url: str


class PublishedObjectResolveRequest(BaseModel):
    """Require exactly one published-object lookup key."""

    object_id: str | None = None
    activity_id: str | None = None

    @model_validator(mode="after")
    def validate_lookup(self) -> "PublishedObjectResolveRequest":
        """Reject ambiguous or empty published-object lookup requests."""
        if (self.object_id is None) == (self.activity_id is None):
            raise ValueError("exactly one of object_id or activity_id is required")
        return self


class MessageMappingResolveRequest(BaseModel):
    """Carry one ActivityPub object identifier for mapping resolution."""

    object_id: str


class ConversationTargetResponse(BaseModel):
    """Expose only non-secret routing identity for one known conversation object."""

    publication_context: str
    root_object_id: str
    actor_id: str | None


class BridgeActorKeyResponse(BaseModel):
    actor_url: str
    key_id: str
    key_format: str
    algorithm: str
    public_key_data: str
    private_key_data: str


class UserActorProfileFieldResponse(BaseModel):
    """Expose one ordered public profile metadata row."""

    name: str
    value: str


class UserActorMediaResponse(BaseModel):
    """Expose only public media rendering metadata to the gateway."""

    url: str
    content_type: str
    width: int
    height: int


class UserActorResponse(BaseModel):
    activitypub_username: str
    actor_url: str
    inbox_url: str
    outbox_url: str
    followers_url: str
    public_key_pem: str
    private_key_pem: str
    display_name: str
    summary: str | None
    fields: list[UserActorProfileFieldResponse]
    avatar: UserActorMediaResponse | None
    header: UserActorMediaResponse | None


class UserFollowersResponse(BaseModel):
    """Expose one bounded page of public follower actor identities."""

    followers_url: str
    total_items: int
    page: int
    page_size: int
    actor_ids: list[str]


class CommunityFollowersResponse(BaseModel):
    """Expose accepted community follower actor IDs without delivery metadata."""

    followers_url: str
    total_items: int
    page: int
    page_size: int
    actor_ids: list[str]


class CommunityActorResponse(BaseModel):
    slug: str
    actor_url: str
    inbox_url: str
    outbox_url: str
    followers_url: str
    display_name: str
    summary: str | None
    public_key_pem: str
    private_key_pem: str


class CommunityDiscoveryItem(BaseModel):
    id: int
    slug: str
    display_name: str
    summary: str | None
    actor_url: str


class CommunityDiscoveryResponse(BaseModel):
    items: list[CommunityDiscoveryItem]


class RemoteSubscriberItem(BaseModel):
    remote_actor_id: str
    remote_inbox_url: str
    follow_activity_id: str
    status: str


class RemoteSubscriberListResponse(BaseModel):
    items: list[RemoteSubscriberItem]


class PublishedObjectResponse(BaseModel):
    actor_username: str
    actor_url: str
    publication_context: str
    community_actor_url: str | None
    activity_id: str
    object_id: str
    kind: str
    title: str | None
    body_markdown: str
    in_reply_to_object_id: str | None
    published_at: str
    discord_guild_id: int | None
    discord_channel_id: int | None
    discord_message_id: int | None


class UserOutboxPageResponse(BaseModel):
    """Expose one fixed-size canonical User Actor outbox slice."""

    outbox_url: str
    total_items: int
    page: int
    page_size: int
    items: list[PublishedObjectResponse]


class MessageMappingResponse(BaseModel):
    source_platform: str
    source_id: str
    activity_id: str
    object_id: str
    actor_url: str
    publication_context: str
    community_actor_url: str | None
    discord_guild_id: int | None
    discord_channel_id: int | None
    discord_message_id: int | None


class ChannelCommunitySubscriptionItem(BaseModel):
    community_actor_url: str
    follow_activity_id: str
    status: str


class ChannelCommunitySubscriptionListResponse(BaseModel):
    items: list[ChannelCommunitySubscriptionItem]


def create_internal_fedify_router(runtime: Runtime) -> APIRouter:
    """Build the private read router consumed only by Fedify Gateway."""
    router = APIRouter(prefix="/internal/fedify")
    actor_keys = ActorKeyService(runtime.database)

    def authorize(authorization: str | None) -> None:
        """Apply the shared internal trust boundary before repository access."""
        validate_internal_bearer(
            authorization=authorization,
            shared_secret=runtime.settings.fedify_shared_secret,
        )

    def no_store(response: Response) -> None:
        """Prevent intermediaries from caching internal read responses."""
        response.headers["Cache-Control"] = "no-store"

    def published_object_payload(row: object) -> PublishedObjectResponse:
        """Map the durable public object contract without delivery metadata."""
        return PublishedObjectResponse(
            actor_username=row.actor_username,
            actor_url=row.actor_url,
            publication_context=row.publication_context,
            community_actor_url=row.community_actor_url,
            activity_id=row.activity_id,
            object_id=row.object_id,
            kind=row.kind,
            title=row.title,
            body_markdown=row.body_markdown,
            in_reply_to_object_id=row.in_reply_to_object_id,
            published_at=row.published_at.isoformat(),
            # MULTIPLATFORM_DISCORD_PROJECTION_TEMP: Existing Fedify JSON DTO keeps Discord fields until the gateway contract is versioned.
            discord_guild_id=row.discord_guild_id,
            discord_channel_id=row.discord_channel_id,
            discord_message_id=row.discord_message_id,
        )

    @router.get("/actors/bridge/key", response_model=BridgeActorKeyResponse)
    def bridge_actor_key(
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> BridgeActorKeyResponse:
        """Return the persisted bridge actor signing keypair."""
        authorize(authorization)
        no_store(response)
        row = runtime.database.bridge_actor_keys.get()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Bridge actor key is not initialized",
            )
        try:
            material = actor_keys.get_bridge_actor_keys()
        except LookupError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Bridge actor key is not initialized",
            ) from exc
        return BridgeActorKeyResponse(
            actor_url=row.actor_url,
            key_id=row.key_id,
            key_format=material.key_format,
            algorithm=material.algorithm,
            public_key_data=material.public_key_data,
            private_key_data=material.private_key_data,
        )

    @router.get("/actors/users/{username}", response_model=UserActorResponse)
    def user_actor(
        username: str,
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> UserActorResponse:
        """Return one registered user actor identity and PEM keypair."""
        authorize(authorization)
        no_store(response)
        row = runtime.database.users.get_user_by_activitypub_username(username)
        if row is None:
            raise HTTPException(status_code=404, detail="User actor not found")
        profile = runtime.database.user_profiles.get(row.id)
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="User profile invariant failed",
            )

        def media_payload(
            asset: ProfileMediaAsset | None,
        ) -> UserActorMediaResponse | None:
            """Map a persisted asset without exposing storage ownership details."""
            if asset is None:
                return None
            base = runtime.settings.normalized_public_bridge_base_url.rstrip("/")
            return UserActorMediaResponse(
                url=f"{base}/media/profile/{asset.public_id}",
                content_type=asset.content_type,
                width=asset.width,
                height=asset.height,
            )

        return UserActorResponse(
            activitypub_username=row.activitypub_username,
            actor_url=row.actor_url,
            inbox_url=row.inbox_url,
            outbox_url=row.outbox_url,
            followers_url=row.followers_url,
            public_key_pem=row.public_key_pem,
            private_key_pem=row.private_key_pem,
            display_name=profile.display_name,
            summary=profile.summary,
            fields=[
                UserActorProfileFieldResponse(name=name, value=value)
                for name, value in profile.fields
            ],
            avatar=media_payload(profile.avatar),
            header=media_payload(profile.header),
        )

    @router.get(
        "/actors/users/{username}/followers", response_model=UserFollowersResponse
    )
    def user_followers(
        username: str,
        response: Response,
        page: int = 1,
        page_size: int = 40,
        authorization: str | None = Header(default=None),
    ) -> UserFollowersResponse:
        """Return one bounded page of accepted User Actor follower IDs."""
        authorize(authorization)
        no_store(response)
        if page < 1 or page_size != 40:
            raise HTTPException(
                status_code=400,
                detail="page must be positive and page_size must equal 40",
            )
        user = runtime.database.users.get_user_by_activitypub_username(username)
        if user is None:
            raise HTTPException(status_code=404, detail="User actor not found")
        return UserFollowersResponse(
            followers_url=user.followers_url,
            total_items=runtime.database.user_actor_followers.count_accepted(
                user_id=user.id
            ),
            page=page,
            page_size=page_size,
            actor_ids=runtime.database.user_actor_followers.list_accepted_actor_ids(
                user_id=user.id, offset=(page - 1) * page_size, limit=page_size
            ),
        )

    def community_payload(row: object) -> CommunityActorResponse:
        """Serialize one local-community row into the internal DTO."""
        return CommunityActorResponse(
            slug=row.slug,
            actor_url=row.actor_url,
            inbox_url=row.inbox_url,
            outbox_url=row.outbox_url,
            followers_url=row.followers_url,
            display_name=row.display_name,
            summary=row.summary,
            public_key_pem=row.public_key_pem,
            private_key_pem=row.private_key_pem,
        )

    @router.get(
        "/actors/users/{username}/outbox", response_model=UserOutboxPageResponse
    )
    def user_outbox(
        username: str,
        response: Response,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=40),
        authorization: str | None = Header(default=None),
    ) -> UserOutboxPageResponse:
        """Return one canonical public authored timeline page for a user."""
        authorize(authorization)
        no_store(response)
        if page_size != 40:
            raise HTTPException(status_code=422, detail="page_size must equal 40")
        user = runtime.database.users.get_user_by_activitypub_username(username)
        if user is None:
            raise HTTPException(status_code=404, detail="User actor not found")
        slice_ = runtime.database.activitypub_objects.load_user_outbox_slice(
            user=user, offset=(page - 1) * page_size, limit=page_size
        )
        return UserOutboxPageResponse(
            outbox_url=user.outbox_url,
            total_items=slice_.total_items,
            page=page,
            page_size=page_size,
            items=[published_object_payload(item) for item in slice_.items],
        )

    @router.get("/actors/communities/{slug}", response_model=CommunityActorResponse)
    def community_actor(
        slug: str,
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> CommunityActorResponse:
        """Return one local-community actor by slug."""
        authorize(authorization)
        no_store(response)
        row = runtime.database.local_communities.get_local_community_by_slug(slug)
        if row is None:
            raise HTTPException(status_code=404, detail="Community actor not found")
        return community_payload(row)

    @router.get(
        "/actors/communities/{slug}/followers",
        response_model=CommunityFollowersResponse,
    )
    def community_followers(
        slug: str,
        response: Response,
        page: int = 1,
        page_size: int = 40,
        authorization: str | None = Header(default=None),
    ) -> CommunityFollowersResponse:
        """Return one bounded page of accepted community follower IDs."""
        authorize(authorization)
        no_store(response)
        if page < 1 or page_size != 40:
            raise HTTPException(
                status_code=400,
                detail="page must be positive and page_size must equal 40",
            )
        community = runtime.database.local_communities.get_local_community_by_slug(slug)
        if community is None:
            raise HTTPException(status_code=404, detail="Community actor not found")
        return CommunityFollowersResponse(
            followers_url=community.followers_url,
            total_items=runtime.database.remote_subscribers.count_accepted_for_community(
                local_community_id=community.id
            ),
            page=page,
            page_size=page_size,
            actor_ids=runtime.database.remote_subscribers.list_accepted_actor_ids_for_community(
                local_community_id=community.id,
                offset=(page - 1) * page_size,
                limit=page_size,
            ),
        )

    @router.post("/actors/communities/resolve", response_model=CommunityActorResponse)
    def resolve_community_actor(
        request: ActorUrlRequest,
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> CommunityActorResponse:
        """Return one local-community actor by canonical actor URL."""
        authorize(authorization)
        no_store(response)
        row = runtime.database.local_communities.get_local_community_by_actor_url(
            request.actor_url
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Community actor not found")
        return community_payload(row)

    @router.get("/communities", response_model=CommunityDiscoveryResponse)
    def communities(
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> CommunityDiscoveryResponse:
        """Return public local-community discovery rows in stable display order."""
        authorize(authorization)
        no_store(response)
        rows = runtime.database.local_communities.list_local_communities()
        rows.sort(key=lambda row: (row.display_name.lower(), row.slug.lower(), row.id))
        return CommunityDiscoveryResponse(
            items=[
                CommunityDiscoveryItem(
                    id=row.id,
                    slug=row.slug,
                    display_name=row.display_name,
                    summary=row.summary,
                    actor_url=row.actor_url,
                )
                for row in rows
            ]
        )

    @router.post(
        "/communities/subscribers", response_model=RemoteSubscriberListResponse
    )
    def community_subscribers(
        request: ActorUrlRequest,
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> RemoteSubscriberListResponse:
        """Return accepted remote subscribers for one local-community actor."""
        authorize(authorization)
        no_store(response)
        community = runtime.database.local_communities.get_local_community_by_actor_url(
            request.actor_url
        )
        if community is None:
            raise HTTPException(status_code=404, detail="Community actor not found")
        rows = runtime.database.remote_subscribers.list_remote_subscribers(
            community.id, status="accepted"
        )
        return RemoteSubscriberListResponse(
            items=[
                RemoteSubscriberItem(
                    remote_actor_id=row.remote_actor_id,
                    remote_inbox_url=row.remote_inbox_url,
                    follow_activity_id=row.follow_activity_id,
                    status=row.status,
                )
                for row in rows
            ]
        )

    @router.post("/published-objects/resolve", response_model=PublishedObjectResponse)
    def resolve_published_object(
        request: PublishedObjectResolveRequest,
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> PublishedObjectResponse:
        """Resolve one gateway-published object by object or activity id."""
        authorize(authorization)
        no_store(response)
        if request.object_id is not None:
            row = runtime.database.activitypub_objects.get_published_activity_object_by_object_id(
                request.object_id
            )
        else:
            row = runtime.database.activitypub_objects.get_published_activity_object_by_activity_id(
                request.activity_id or ""
            )
        if row is None:
            raise HTTPException(status_code=404, detail="Published object not found")
        return PublishedObjectResponse(
            actor_username=row.actor_username,
            actor_url=row.actor_url,
            publication_context=row.publication_context,
            community_actor_url=row.community_actor_url,
            activity_id=row.activity_id,
            object_id=row.object_id,
            kind=row.kind,
            title=row.title,
            body_markdown=row.body_markdown,
            in_reply_to_object_id=row.in_reply_to_object_id,
            published_at=_isoformat(row.published_at),
            # MULTIPLATFORM_DISCORD_PROJECTION_TEMP: Existing Fedify JSON DTO keeps Discord fields until the gateway contract is versioned.
            discord_guild_id=row.discord_guild_id,
            discord_channel_id=row.discord_channel_id,
            discord_message_id=row.discord_message_id,
        )

    @router.post(
        "/conversation-targets/resolve", response_model=ConversationTargetResponse
    )
    def resolve_conversation_target(
        request: MessageMappingResolveRequest,
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> ConversationTargetResponse:
        """Resolve bridge-owned or retained inbound objects before community routing."""
        authorize(authorization)
        no_store(response)
        resolved = (
            runtime.database.personal_feed_conversations.resolve_conversation_object(
                request.object_id
            )
        )
        if resolved is None:
            raise HTTPException(status_code=404, detail="Conversation object not found")
        context, root_object_id, actor_id = resolved
        return ConversationTargetResponse(
            publication_context=context,
            root_object_id=root_object_id,
            actor_id=actor_id,
        )

    @router.post("/message-mappings/resolve", response_model=MessageMappingResponse)
    def resolve_message_mapping(
        request: MessageMappingResolveRequest,
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> MessageMappingResponse:
        """Resolve one generic message mapping by ActivityPub object id."""
        authorize(authorization)
        no_store(response)
        row = runtime.database.message_mappings.get_message_mapping_by_object_id(
            request.object_id
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Message mapping not found")
        return MessageMappingResponse(
            source_platform=row.source_platform,
            source_id=row.source_id,
            activity_id=row.activity_id,
            object_id=row.object_id,
            actor_url=row.actor_url,
            publication_context=row.publication_context,
            community_actor_url=row.community_actor_url,
            # MULTIPLATFORM_DISCORD_PROJECTION_TEMP: Existing Fedify JSON DTO keeps Discord fields until the gateway contract is versioned.
            discord_guild_id=row.discord_guild_id,
            discord_channel_id=row.discord_channel_id,
            discord_message_id=row.discord_message_id,
        )

    @router.get(
        "/channel-community-subscriptions",
        response_model=ChannelCommunitySubscriptionListResponse,
    )
    def channel_community_subscriptions(
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> ChannelCommunitySubscriptionListResponse:
        """Return operator subscription rows that own a persisted Follow id."""
        authorize(authorization)
        no_store(response)
        rows = runtime.database.remote_subscriptions.get_all_subscriptions()
        return ChannelCommunitySubscriptionListResponse(
            items=[
                ChannelCommunitySubscriptionItem(
                    community_actor_url=row.lemmy_community_actor_id,
                    follow_activity_id=row.follow_activity_id,
                    status=row.status,
                )
                for row in rows
                if row.follow_activity_id is not None
            ]
        )

    return router


def _isoformat(value: datetime) -> str:
    """Serialize persisted timestamps consistently for TypeScript consumers."""
    return value.isoformat()
