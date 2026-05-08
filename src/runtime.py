from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .db import Database
from .discord_bot import BridgeBot
from .fedify_gateway_client import FedifyGatewayClient
from .lemmy_client import LemmyClient


@dataclass(slots=True)
class Runtime:
    # Runtime groups the shared long-lived services that request handlers and
    # Discord callbacks need to access without rebuilding them.
    settings: Settings
    database: Database
    lemmy: LemmyClient
    fedify_gateway: FedifyGatewayClient
    bot: BridgeBot
