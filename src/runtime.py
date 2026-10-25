"""Shared long-lived services for the bridge process lifetime."""

from __future__ import annotations

from dataclasses import dataclass

from .community_sync.runtime import CommunityRuntime
from .bridge_policy import BridgePolicyService
from .config import Settings
from .db import Database
from .discord_bot import BridgeBot
from .discord_adapter import DiscordAdapter
from .platform_adapters import PlatformAdapterRegistry
from .content_sync.platform_mutations import PlatformPhysicalMutationCoordinator
from .discord_oauth_client import DiscordOAuthClient
from .content_publish_service import ContentPublishService
from .fedify_gateway_client import FedifyGatewayClient
from .local_communities.runtime import LocalCommunityRuntime
from .registration_service import RegistrationService
from .profile_media import FileLock, ProfileMediaStore
from .user_profiles import UserProfileService
from .user_actor_followers import UserActorFollowService
from .user_actor_delivery import UserActorDeliveryService
from .user_actor_delivery_worker import UserActorDeliveryWorker
from .personal_feed_conversations import PersonalFeedConversationRuntime
from .personal_feed_discord_delivery_worker import PersonalFeedDiscordDeliveryWorker


@dataclass(slots=True)
class Runtime:
    """Group the shared long-lived services that request handlers and Discord callbacks need.

    All fields are constructed once in app.py and shared across the full bridge
    process lifetime. No field is optional; absence of a dependency is a startup error.
    """

    # Runtime groups the shared long-lived services that request handlers and
    # Discord callbacks need to access without rebuilding them.
    settings: Settings
    database: Database
    fedify_gateway: FedifyGatewayClient
    discord_oauth_client: DiscordOAuthClient
    content_publish_service: ContentPublishService
    registration_service: RegistrationService
    bridge_policy_service: BridgePolicyService
    bot: BridgeBot
    discord_adapter: DiscordAdapter
    adapter_registry: PlatformAdapterRegistry
    physical_mutation_coordinator: PlatformPhysicalMutationCoordinator
    community_runtime: CommunityRuntime
    local_community_runtime: LocalCommunityRuntime
    profile_media_store: ProfileMediaStore
    user_profile_service: UserProfileService
    user_actor_follow_service: UserActorFollowService
    user_actor_delivery_service: UserActorDeliveryService
    user_actor_delivery_worker: UserActorDeliveryWorker
    personal_feed_conversation_runtime: PersonalFeedConversationRuntime
    personal_feed_discord_delivery_worker: PersonalFeedDiscordDeliveryWorker
    deployment_state_lease: FileLock

    async def close(self) -> None:
        """Join active owners and report all failures without masking the first.

        Workers own joining their task finalizers before `stop()` returns or
        raises. The runtime must still attempt the remaining ordered cleanup,
        retaining cancellation and every independent failure for the caller.
        """
        errors: list[BaseException] = []

        # Close the gate before the first await, even when shutdown itself was
        # entered due to TaskGroup failure. Every later owner is attempted.
        try:
            self.discord_adapter.close_ingress_admission()
        except BaseException as exc:
            errors.append(exc)

        for operation in (
            self.discord_adapter.drain_ingress,
            self.personal_feed_discord_delivery_worker.stop,
            self.user_actor_delivery_worker.stop,
            self.discord_adapter.quiesce,
            self.discord_oauth_client.close,
            self.fedify_gateway.close,
        ):
            try:
                await operation()
            except BaseException as exc:
                # An earlier stop failure should not be replaced by a later
                # Fedify/DB failure. Cancellation is preserved, not swallowed.
                errors.append(exc)

        for operation in (self.database.close, self.deployment_state_lease.close):
            try:
                operation()
            except BaseException as exc:
                errors.append(exc)

        if len(errors) == 1:
            raise errors[0]
        if errors:
            # The earliest failure remains first and every subsequent error is
            # visible; Python 3.12 ExceptionGroup preserves traceback chains.
            raise BaseExceptionGroup('Runtime shutdown failures', errors)
