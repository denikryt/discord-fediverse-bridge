"""Shared DiscordOps inputs, preconditions, and command-access policies.

This module owns reusable access declarations that are consumed by both
Discord command ingress and domain operations. It intentionally depends only
on primitive project types and DiscordOps, never on the Discord SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from discordops import PolicyDefinition, Precondition

from ..config import Settings
from ..db import Database

GUILD_ONLY_MESSAGE = "This command can only be used inside an allowed Discord server."
GUILD_NOT_ALLOWED_MESSAGE = "This Discord server is not allowed to use this bridge bot."
REGISTRATION_REQUIRED_MESSAGE = "You must register with the bridge before using this command. Use `/register` first."


class RegisteredDiscordUserInput(Protocol):
    """Structural contract for inputs that can resolve a registered bridge user."""

    def get_bridge_user(self) -> object | None:
        """Return the registered bridge user or ``None`` when registration is absent."""
        ...


@dataclass(slots=True)
class CommandAccessInput:
    """Carry primitive command context and memoized registration state.

    The input contains no Discord SDK object, so the same policies can be used
    by slash commands, modal submissions, autocomplete, and focused tests.
    Registration lookup is memoized per interaction input to avoid duplicate
    repository reads when one policy or caller resolves the user repeatedly.
    """

    settings: Settings
    database: Database | Any | None
    discord_guild_id: int | None
    discord_user_id: str
    member_can_manage_guild: bool = False
    _bridge_user: object | None = field(default=None, init=False, repr=False)
    _bridge_user_loaded: bool = field(default=False, init=False, repr=False)

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
    allowlist = value.settings.discord_guild_allowlist
    return not allowlist or str(value.discord_guild_id) in allowlist


def _member_can_manage_guild(value: CommandAccessInput) -> bool:
    """Return whether the invoking guild member has Discord Manage Guild."""
    return value.member_can_manage_guild


def _discord_user_is_registered(value: RegisteredDiscordUserInput) -> bool:
    """Return whether the input resolves an existing registered bridge user."""
    return value.get_bridge_user() is not None


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

MANAGE_GUILD_REQUIRED = Precondition(
    name="missing_manage_guild",
    message="You need the Manage Server permission to manage this server invite.",
    predicate=_member_can_manage_guild,
)

DISCORD_USER_REGISTERED = Precondition(
    name="discord_user_not_registered",
    message=REGISTRATION_REQUIRED_MESSAGE,
    predicate=_discord_user_is_registered,
)

# Named compositions keep command handlers declarative while preserving one
# source of truth for atomic preconditions and their short-circuit order.
GUILD_COMMAND_ACCESS = PolicyDefinition(
    name="guild_command_access",
    preconditions=(GUILD_CONTEXT_REQUIRED, GUILD_ALLOWLISTED),
)

REGISTERED_GUILD_COMMAND_ACCESS = PolicyDefinition(
    name="registered_guild_command_access",
    preconditions=(GUILD_CONTEXT_REQUIRED, GUILD_ALLOWLISTED, DISCORD_USER_REGISTERED),
)

MANAGE_GUILD_COMMAND_ACCESS = PolicyDefinition(
    name="manage_guild_command_access",
    preconditions=(GUILD_CONTEXT_REQUIRED, GUILD_ALLOWLISTED, MANAGE_GUILD_REQUIRED),
)
