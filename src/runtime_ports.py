"""Structural runtime ports for process composition boundaries.

The types in this module are intentionally small and non-secret.  They describe
only the cross-component capabilities that cannot be represented by direct
constructor injection without recreating the bot/router/runtime cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class DiscordDeliveryClientPort(Protocol):
    """Expose only Discord delivery operations used by community runtimes."""

    async def wait_until_bridge_ready(self) -> None:
        """Wait until the Discord adapter has completed its ready callback."""

    async def fetch_forum_channel(self, channel_id: int) -> object:
        """Fetch a Discord forum channel by snowflake id."""

    async def get_thread_by_id(self, thread_id: int) -> object | None:
        """Return a Discord thread by snowflake id when it is still reachable."""


class DiscordUserLookupPort(Protocol):
    """Expose only the user fetch needed for User Actor follow notifications."""

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
        target_inbox_urls: list[str] | None = None,
    ) -> str:
        """Persist one desired Update and return the existing admission outcome."""

    async def submit_delete(
        self,
        *,
        object_id: str,
        target_inbox_urls: list[str] | None = None,
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
