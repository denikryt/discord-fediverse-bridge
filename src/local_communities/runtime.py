"""Runtime facade for the Discord-backed local-community mode.

LocalCommunityRuntime remains the public orchestration API used by Discord and
ActivityPub callers.  Focused local-community collaborators own create, inbound,
mutation, follower, rendering, and fanout behavior while this facade keeps the
stable routing and disabled-community gates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..bridge_policy import BridgePolicyService
from ..content_publish_service import ContentPublishService
from ..db import Database
from ..fedify_gateway_client import FedifyGatewayClient
from ..local_community_lifecycle import evaluate_local_community_lifecycle
from ..runtime_ports import DiscordDeliveryClientPort, DiscordMutationContext
from .discord_create import LocalCommunityDiscordCreateHandler
from .discord_surface_fanout import LocalCommunitySurfaceFanout
from .federation_fanout import LocalCommunityFederationFanout
from .followers import LocalCommunityFollowerHandler
from .inbound_create import LocalCommunityInboundCreateHandler
from .mutations import LocalCommunityMutationHandler
from .participant_routing import resolve_local_community_source_for_forum
from .results import LocalCommunityRuntimeResult

if TYPE_CHECKING:
    from ..activitypub_handlers import HandlerResult


class LocalCommunityRuntime:
    """Own local-community dispatch policy around a narrow stable runtime API."""

    def __init__(
        self,
        *,
        database: Database,
        fedify_gateway: FedifyGatewayClient,
        content_publish_service: ContentPublishService,
        bridge_prefix: str,
        bridge_policy_service: BridgePolicyService,
        bot: DiscordDeliveryClientPort | None = None,
    ) -> None:
        """Initialise the facade and focused process-local collaborators."""
        self.database = database
        self.fedify_gateway = fedify_gateway
        self.content_publish_service = content_publish_service
        self.bridge_prefix = bridge_prefix
        self.bridge_policy_service = bridge_policy_service
        self.bot = bot
        self.federation_fanout = LocalCommunityFederationFanout(
            database=database,
            fedify_gateway=fedify_gateway,
            policy_service=bridge_policy_service,
        )
        self.surface_fanout = LocalCommunitySurfaceFanout(
            database=database,
            policy_service=bridge_policy_service,
            discord_client_provider=lambda: self.bot,
        )
        self.discord_create_handler = LocalCommunityDiscordCreateHandler(
            database=database,
            content_publish_service=content_publish_service,
            surface_fanout=self.surface_fanout,
        )
        self.inbound_create_handler = LocalCommunityInboundCreateHandler(
            database=database,
            surface_fanout=self.surface_fanout,
            federation_fanout=self.federation_fanout,
            discord_client_provider=lambda: self.bot,
        )
        self.mutation_handler = LocalCommunityMutationHandler(
            database=database,
            fedify_gateway=fedify_gateway,
            bridge_policy_service=bridge_policy_service,
            surface_fanout=self.surface_fanout,
            federation_fanout=self.federation_fanout,
            is_community_id_disabled=self._is_community_id_disabled,
        )
        self.follower_handler = LocalCommunityFollowerHandler(
            database=database,
            fedify_gateway=fedify_gateway,
            bridge_policy_service=bridge_policy_service,
        )

    def bind_discord_client(self, bot: DiscordDeliveryClientPort) -> None:
        """Attach the Discord delivery client once after the bot/router cycle exists."""
        if self.bot is not None:
            raise RuntimeError("LocalCommunityRuntime Discord client is already configured")
        self.bot = bot

    def _disabled_result_if_needed(
        self, local_community: object
    ) -> LocalCommunityRuntimeResult | None:
        """Return an ignored result when a community is operationally disabled."""
        decision = evaluate_local_community_lifecycle(local_community)
        if decision.allowed:
            return None
        return LocalCommunityRuntimeResult(status="ignored", reason=decision.reason)

    def _is_community_id_disabled(self, local_community_id: int | None) -> bool:
        """Return whether a resolved canonical community id is disabled."""
        if local_community_id is None:
            return False
        community = self.database.local_communities.get_local_community_by_id(
            local_community_id
        )
        if community is None:
            return False
        return not evaluate_local_community_lifecycle(community).allowed

    async def handle_discord_thread_create(
        self,
        *,
        thread: object,
        starter_message: object,
    ) -> LocalCommunityRuntimeResult:
        """Publish one Discord thread starter into a local community as a post."""
        source = resolve_local_community_source_for_forum(
            self.database, getattr(thread, "parent_id", None)
        )
        if source is None:
            return LocalCommunityRuntimeResult(
                status="ignored", reason="not_local_community"
            )
        disabled = self._disabled_result_if_needed(source.local_community)
        if disabled is not None:
            return disabled
        if source.source_kind == "local_subscriber":
            return await self.discord_create_handler.handle_local_subscriber_thread_create(
                thread=thread,
                starter_message=starter_message,
                local_community=source.local_community,
                local_subscriber=source.local_subscriber,
            )
        return await self.discord_create_handler.handle_host_thread_create(
            thread=thread,
            starter_message=starter_message,
            local_community=source.local_community,
        )

    async def handle_discord_message(
        self, *, message: object
    ) -> LocalCommunityRuntimeResult:
        """Publish one Discord reply inside a local-community thread as a comment."""
        thread = getattr(message, "channel")
        source = resolve_local_community_source_for_forum(
            self.database, getattr(thread, "parent_id", None)
        )
        if source is None:
            return LocalCommunityRuntimeResult(
                status="ignored", reason="not_local_community"
            )
        disabled = self._disabled_result_if_needed(source.local_community)
        if disabled is not None:
            return disabled
        if source.source_kind == "local_subscriber":
            return await self.discord_create_handler.handle_local_subscriber_message(
                message=message,
                local_community=source.local_community,
                local_subscriber=source.local_subscriber,
            )
        return await self.discord_create_handler.handle_host_message(
            message=message, local_community=source.local_community
        )

    async def handle_inbound_post(
        self, event: object, runtime: object
    ) -> HandlerResult:
        """Mirror one remote top-level post into a new Discord forum thread."""
        return await self.inbound_create_handler.handle_post(event, runtime)

    async def handle_inbound_comment(
        self, event: object, runtime: object
    ) -> HandlerResult:
        """Mirror one remote comment into the mapped Discord thread."""
        return await self.inbound_create_handler.handle_comment(event, runtime)

    async def handle_discord_message_edit(
        self,
        *,
        message_id: int,
        new_content: str,
        mutation_context: DiscordMutationContext | None = None,
        runtime: object | None = None,
        author_display_name: str = "",
        edited_timestamp=None,
    ) -> None:
        """Propagate one Discord-authored local-community edit to participants."""
        await self.mutation_handler.handle_discord_message_edit(
            message_id=message_id,
            new_content=new_content,
            mutation_context=mutation_context,
            runtime=runtime,
            author_display_name=author_display_name,
            edited_timestamp=edited_timestamp,
        )

    async def handle_discord_message_delete(
        self,
        *,
        message_id: int,
        mutation_context: DiscordMutationContext | None = None,
        runtime: object | None = None,
    ) -> None:
        """Propagate one Discord-authored local-community delete to participants."""
        await self.mutation_handler.handle_discord_message_delete(
            message_id=message_id,
            mutation_context=mutation_context,
            runtime=runtime,
        )

    async def handle_inbound_post_update(
        self, event: object, runtime: object
    ) -> HandlerResult:
        """Edit the starter message for one inbound remote post update."""
        return await self.mutation_handler.handle_inbound_post_update(event, runtime)

    async def handle_inbound_post_delete(
        self, event: object, runtime: object
    ) -> HandlerResult:
        """Mark the starter message deleted for one inbound remote post delete."""
        return await self.mutation_handler.handle_inbound_post_delete(event, runtime)

    async def handle_inbound_comment_update(
        self, event: object, runtime: object
    ) -> HandlerResult:
        """Edit the mirrored Discord copy for one inbound remote comment update."""
        return await self.mutation_handler.handle_inbound_comment_update(event, runtime)

    async def handle_inbound_comment_delete(
        self, event: object, runtime: object
    ) -> HandlerResult:
        """Mark the mirrored Discord copy deleted for one inbound remote comment delete."""
        return await self.mutation_handler.handle_inbound_comment_delete(event, runtime)

    async def handle_follow_request(
        self,
        *,
        local_community_actor_id: str,
        remote_actor_id: str,
        remote_inbox_url: str,
        follow_activity_id: str,
    ) -> HandlerResult:
        """Persist and accept one remote follow request for a local community."""
        return await self.follower_handler.handle_follow_request(
            local_community_actor_id=local_community_actor_id,
            remote_actor_id=remote_actor_id,
            remote_inbox_url=remote_inbox_url,
            follow_activity_id=follow_activity_id,
        )

    async def handle_unfollow_request(
        self,
        *,
        local_community_actor_id: str,
        remote_actor_id: str,
        follow_activity_id: str | None,
    ) -> HandlerResult:
        """Remove one remote actor from a local community remote-subscriber set."""
        return await self.follower_handler.handle_unfollow_request(
            local_community_actor_id=local_community_actor_id,
            remote_actor_id=remote_actor_id,
            follow_activity_id=follow_activity_id,
        )
