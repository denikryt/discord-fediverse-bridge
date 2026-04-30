from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .db import Database
from .discord_bot import BridgeBot
from .lemmy_client import LemmyClient


@dataclass(slots=True)
class Runtime:
    settings: Settings
    database: Database
    lemmy: LemmyClient
    bot: BridgeBot
