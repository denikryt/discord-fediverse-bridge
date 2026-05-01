from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .db import Database
from .discord_bot import BridgeBot
from .lemmy_client import LemmyClient


@dataclass(slots=True)
class Runtime:
    # Runtime groups the shared long-lived services that request handlers and
    # Discord callbacks need to access without rebuilding them.
    settings: Settings
    database: Database
    lemmy: LemmyClient
    bot: BridgeBot
