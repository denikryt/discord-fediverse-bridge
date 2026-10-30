"""Shared long-lived services for the bridge process lifetime."""

from __future__ import annotations

from dataclasses import dataclass, field

from .runtime_cleanup import quarantine, release_quarantine
from .community_sync.runtime import CommunityRuntime
from .bridge_policy import BridgePolicyService
from .config import Settings
from .db import Database
from .discord_bot import BridgeBot
from .discord_adapter import DiscordAdapter
from .fluxer_adapter import FluxerAdapter
from .fluxer_ingress import FluxerInboundHandler
from .fluxer_rpc_client import FluxerRpcClient
from .platform_adapters import PlatformAdapterRegistry
from .content_sync.platform_mutations import PlatformPhysicalMutationCoordinator
from .content_sync.platform_delivery_scheduler import (
    ConnectorDispatchCapacity, PlatformPhysicalDeliveryScheduler,
)
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

    Required services are constructed once in app.py and shared for the bridge
    lifetime. Fluxer owners are absent only when the connector is disabled.
    Failed external owners remain strongly retained until process termination.
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
    fluxer_adapter: FluxerAdapter | None
    fluxer_rpc: FluxerRpcClient | None
    fluxer_ingress: FluxerInboundHandler | None
    adapter_registry: PlatformAdapterRegistry
    physical_mutation_coordinator: PlatformPhysicalMutationCoordinator
    physical_delivery_scheduler: PlatformPhysicalDeliveryScheduler
    physical_dispatch_gate: ConnectorDispatchCapacity
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
    runtime_lifetime_lease: FileLock
    _unclosed_owners: list[tuple[object, BaseException]] = field(default_factory=list, init=False, repr=False)

    def retain_unclosed_owner(self, owner: object, failure: BaseException) -> None:
        """Keep an unjoined external service alive and prohibit both lease releases.

        HTTP belongs to app.main, outside the Runtime service graph. If its
        shutdown cannot be confirmed, an otherwise successful SQLite close must
        not authorize restart or restore while callbacks might still be alive.
        """
        self._unclosed_owners.append((owner, failure))

    async def close(self) -> None:
        """Join active owners and report all failures without masking the first.

        Workers own joining their task finalizers before `stop()` returns or
        raises. The runtime must still attempt the remaining ordered cleanup,
        retaining cancellation and every independent failure for the caller.
        """
        errors = [failure for _owner, failure in self._unclosed_owners]

        # Close the gate before the first await, even when shutdown itself was
        # entered due to TaskGroup failure. Every later owner is attempted.
        try:
            # Close physical admission before any awaits; queued direct callers
            # cannot claim after the runtime enters structured shutdown.
            self.physical_dispatch_gate.close_admission()
        except BaseException as exc:
            errors.append(exc)

        try:
            self.discord_adapter.close_ingress_admission()
        except BaseException as exc:
            errors.append(exc)
        if self.fluxer_ingress is not None:
            # This gate closes before stopping lanes or disposing SQLite: an
            # already admitted HTTP callback remains owned until quiesce joins.
            try:
                self.fluxer_ingress.close_admission()
            except BaseException as exc:
                errors.append(exc)

        for operation in (
            *((self.fluxer_ingress.quiesce,) if self.fluxer_ingress is not None else ()),
            self.discord_adapter.drain_ingress,
            self.physical_delivery_scheduler.stop,
            self.physical_dispatch_gate.drain,
            self.personal_feed_discord_delivery_worker.stop,
            self.user_actor_delivery_worker.stop,
            self.discord_adapter.quiesce,
            *((self.fluxer_adapter.quiesce,) if self.fluxer_adapter is not None else ()),
            *((self.fluxer_rpc.close,) if self.fluxer_rpc is not None else ()),
            self.discord_oauth_client.close,
            self.fedify_gateway.close,
        ):
            try:
                await operation()
            except BaseException as exc:
                # An earlier stop failure should not be replaced by a later
                # Fedify/DB failure. Cancellation is preserved, not swallowed.
                errors.append(exc)

        # SQLite and restore protection share one shutdown boundary. Any failed
        # owner may still use the DB, so neither lease is released on uncertainty.
        try:
            self.database.close()
        except BaseException as exc:
            errors.append(exc)
        if not errors:
            try:
                self.deployment_state_lease.close()
                # Release absolutely last, after confirmed DB and deployment close.
                self.runtime_lifetime_lease.close()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise quarantine(self, errors)
        release_quarantine(self)
