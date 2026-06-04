"""Discord-independent command ingress policies built from DiscordOps conditions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from discordops import PolicyDefinition, Precondition

from .config import Settings
from .db import Database
from .operations.common_preconditions import DISCORD_USER_REGISTERED

GUILD_ONLY_MESSAGE = "This command can only be used inside an allowed Discord server."
GUILD_NOT_ALLOWED_MESSAGE = "This Discord server is not allowed to use this bridge bot."


@dataclass(slots=True)
class CommandAccessInput:
    """Carry primitive command context and memoized registration state.

    The input deliberately contains no Discord SDK object. This keeps policy
    evaluation reusable in commands, modal submissions, autocomplete, and tests.
    """

    settings: Settings | object | None
    database: Database | Any | None
    discord_guild_id: int | None
    discord_user_id: str
    _bridge_user: object | None = field(default=None, init=False, repr=False)
    _bridge_user_loaded: bool = field(default=False, init=False, repr=False)

    def configured_guild_allowlist(self) -> tuple[str, ...]:
        """Return normalized configured guild IDs, tolerating lightweight settings."""
        if self.settings is None:
            return ()
        return tuple(str(entry) for entry in getattr(self.settings, "discord_guild_allowlist", []))

    def get_bridge_user(self) -> object | None:
        """Resolve and memoize the registered bridge user for this command input."""
        if not self._bridge_user_loaded:
            if self.database is None:
                raise RuntimeError("Registered command access requires a database.")
            self._bridge_user = self.database.users.get_user_by_discord_user_id(self.discord_user_id)
            self._bridge_user_loaded = True
        return self._bridge_user


def _has_guild_context(value: CommandAccessInput) -> bool:
    """Return whether the interaction originated from a Discord guild."""
    return value.discord_guild_id is not None


def _guild_is_allowlisted(value: CommandAccessInput) -> bool:
    """Apply unrestricted-empty-list compatibility and configured membership."""
    allowlist = value.configured_guild_allowlist()
    return not allowlist or str(value.discord_guild_id) in allowlist


GUILD_CONTEXT_REQUIRED = Precondition(
    name="no_guild",
    message=GUILD_ONLY_MESSAGE,
    predicate=_has_guild_context,
)

GUILD_ALLOWLISTED = Precondition(
    name="not_allowlisted",
    message=GUILD_NOT_ALLOWED_MESSAGE,
    predicate=_guild_is_allowlisted,
)

GUILD_COMMAND_ACCESS = PolicyDefinition(
    name="guild_command_access",
    preconditions=(GUILD_CONTEXT_REQUIRED, GUILD_ALLOWLISTED),
)

REGISTERED_GUILD_COMMAND_ACCESS = PolicyDefinition(
    name="registered_guild_command_access",
    preconditions=(GUILD_CONTEXT_REQUIRED, GUILD_ALLOWLISTED, DISCORD_USER_REGISTERED),
)
