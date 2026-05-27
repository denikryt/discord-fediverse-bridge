"""Shared long-lived services for the bridge process lifetime."""

from __future__ import annotations

from dataclasses import dataclass

from .community_sync.runtime import CommunityRuntime
from .config import Settings
from .db import Database
from .discord_bot import BridgeBot
from .discord_oauth_client import DiscordOAuthClient
from .content_publish_service import ContentPublishService
from .fedify_gateway_client import FedifyGatewayClient
from .local_communities.runtime import LocalCommunityRuntime
from .registration_service import RegistrationService


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
    bot: BridgeBot
    community_runtime: CommunityRuntime
    local_community_runtime: LocalCommunityRuntime
