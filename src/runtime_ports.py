"""Structural runtime ports for process composition boundaries.

The types in this module are intentionally small and non-secret.  They describe
only the cross-component capabilities that cannot be represented by direct
constructor injection without recreating the bot/router/runtime cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .platform_types import (
    AuthorRef, ChannelRef, ChannelView, ConnectorRef, ContentDocument,
    CreateAttempt, CoordinatorDispatchPermit, CreatedMessage, CreatedThread, DeliveryPresentation,
    MessageRef, MessageView, PlatformAction, PlatformCapabilities,
    PlatformInboundEvent, PermissionResult, ThreadRef, ThreadView,
)


@runtime_checkable
class PlatformLookupPort(Protocol):
    """SDK-free metadata/ACL reads and read-only attempt reconciliation."""

    async def fetch_channel(self, channel: ChannelRef) -> ChannelView:
        """Return actual channel metadata, never a bridge ownership claim."""

    async def fetch_thread(self, thread: ThreadRef) -> ThreadView:
        """Return thread metadata with the observed starter ID or None."""

    async def fetch_message(self, message: MessageRef) -> MessageView:
        """Resolve exact content after a partial Gateway update if accessible."""

    async def reconcile_create(
        self, attempt: CreateAttempt
    ) -> CreatedThread | ThreadRef | CreatedMessage | None:
        """Read a proven receipt or unresolved None; never create or retry."""

    async def check_permissions(
        self, principal: AuthorRef, channel: ChannelRef,
        actions: frozenset[PlatformAction],
    ) -> dict[PlatformAction, PermissionResult]:
        """Report unknown/unsupported distinctly; checks grant no permission."""

    async def capabilities(self, channel: ChannelRef) -> PlatformCapabilities:
        """Return platform feature support independently of member ACLs."""


@runtime_checkable
class PlatformDeliveryPort(Protocol):
    """Physical copy writes; canonical AP state and retry intents belong to core."""

    async def create_forum_thread(
        self, forum: ChannelRef, content: ContentDocument, *, title: str,
        presentation: DeliveryPresentation, attempt: CreateAttempt,
        dispatch_permit: CoordinatorDispatchPermit | None = None,
    ) -> CreatedThread:
        """Create a forum post and return observed thread/starter IDs."""

    async def create_reply_thread(
        self, root: MessageRef, *, title: str, attempt: CreateAttempt,
        dispatch_permit: CoordinatorDispatchPermit | None = None,
    ) -> ThreadRef:
        """Create or adopt a verified unique root-anchored message thread."""

    async def send_message(
        self, destination: ThreadRef | ChannelRef, content: ContentDocument, *,
        reply_to: MessageRef | None, presentation: DeliveryPresentation,
        attempt: CreateAttempt,
        dispatch_permit: CoordinatorDispatchPermit | None = None,
    ) -> CreatedMessage:
        """Send in one immediate container; forbid implicit cross-parent fallback."""

    async def edit_message(self, message: MessageRef, content: ContentDocument) -> None:
        """Edit a physical copy/starter, not a canonical object or thread name."""

    async def delete_message(self, message: MessageRef) -> None:
        """Delete a physical copy/starter, not its containing physical thread."""


@runtime_checkable
class PlatformAdapterPort(PlatformLookupPort, PlatformDeliveryPort, Protocol):
    """One connector's combined read/write transport without SDK ownership."""

    @property
    def connector(self) -> ConnectorRef:
        """Return immutable canonical connector metadata, without secrets."""


@runtime_checkable
class PlatformInboundSinkPort(Protocol):
    """Single future Gateway ingress for normalized connector-scoped events."""

    async def handle_platform_event(self, event: PlatformInboundEvent) -> None:
        """Accept a typed notice; actual routing will be wired in Discord adapter integration."""


# MULTIPLATFORM_LEGACY_DISCORD_PORT_TEMP: Legacy Discord-only runtime ports stay until Discord adapter and durable fan-out integration replaces direct Discord delivery binds with PlatformAdapterPort; remove after all affected callers use the new port.
class DiscordDeliveryClientPort(Protocol):
    """Expose only Discord delivery operations used by community runtimes."""

    async def wait_until_bridge_ready(self) -> None:
        """Wait until the Discord adapter has completed its ready callback."""

    async def fetch_forum_channel(self, channel_id: int) -> object:
        """Fetch a Discord forum channel by snowflake id."""

    async def get_thread_by_id(self, thread_id: int) -> object | None:
        """Return a Discord thread by snowflake id when it is still reachable."""


class DiscordOwnerNoticePort(Protocol):
    """Discord-only owner DM transport, deliberately absent from Platform API."""

    async def send_owner_notice(self, discord_user_id: int, content: str) -> None:
        """Attempt one mention-safe Discord DM; no implicit retry on failure."""

    async def fetch_user(self, user_id: int) -> object:
        """Fetch one Discord user by snowflake id."""


class DiscordChannelLookupPort(Protocol):
    """Expose only channel lookup/fetch operations used by delivery workers."""

    def get_channel(self, channel_id: int) -> object | None:
        """Return a cached Discord channel when available."""

    async def fetch_channel(self, channel_id: int) -> object:
        """Fetch a Discord channel by snowflake id."""


class DeliveryWakePort(Protocol):
    """Wake a process-local delivery scheduler after durable admission."""

    def wake(self) -> None:
        """Notify the existing worker event without taking ownership of the worker."""


class UserActorDeliverySubmitPort(Protocol):
    """Submit durable user-actor content mutations without exposing the service."""

    async def submit_update(
        self,
        *,
        object_id: str,
        title: str | None,
        body_markdown: str,
        edited_timestamp,
    ) -> str:
        """Persist one desired Update and return the existing admission outcome."""

    async def submit_delete(
        self,
        *,
        object_id: str,
    ) -> str:
        """Persist one desired Delete and return the existing admission outcome."""


class FederationDecision(Protocol):
    """Expose the boolean policy decision needed by mutation routing."""

    allowed: bool


class FederationPolicyPort(Protocol):
    """Evaluate federation policy without exposing secret-bearing settings."""

    def federation_decision(self, target: str) -> FederationDecision:
        """Return the current federation decision for one actor or inbox target."""


class LocalCommunityGatewayFallbackPort(Protocol):
    """Render direct local-community gateway fallbacks for compatibility tests."""

    async def update_content(self, request: object) -> object:
        """Send one direct Update request using the existing gateway contract."""

    async def delete_content(self, request: object) -> object:
        """Send one direct Delete request using the existing gateway contract."""


@dataclass(frozen=True, slots=True)
class DiscordMutationContext:
    """Minimal raw edit/delete context shared by router and runtime branches.

    It deliberately does not contain Runtime, BridgeBot, Settings, gateway
    secrets, or arbitrary objects.  Each field maps to a concrete call made by
    the Discord mutation path.
    """

    bridge_policy_service: FederationPolicyPort
    user_actor_delivery_service: UserActorDeliverySubmitPort | None
    user_actor_delivery_wake: DeliveryWakePort
    fedify_gateway: LocalCommunityGatewayFallbackPort
