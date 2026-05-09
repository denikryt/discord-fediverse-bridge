"""Shared long-lived services for the bridge process lifetime."""

from __future__ import annotations

from dataclasses import dataclass

from .community_sync.runtime import CommunityRuntime
from .config import Settings
from .db import Database
from .discord_bot import BridgeBot
from .discord_oauth_client import DiscordOAuthClient
from .discord_publish_service import DiscordPublishService
from .fedify_gateway_client import FedifyGatewayClient
from .lemmy_client import LemmyClient
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
    lemmy: LemmyClient
    fedify_gateway: FedifyGatewayClient
    discord_oauth_client: DiscordOAuthClient
    discord_publish_service: DiscordPublishService
    registration_service: RegistrationService
    bot: BridgeBot
    community_runtime: CommunityRuntime
