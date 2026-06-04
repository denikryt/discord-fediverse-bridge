"""Shared Discord slash-command access guards.

This module owns deployment-level command checks that must run before command-
specific work. It intentionally lives in the command layer because it imports
Discord SDK objects and sends interaction responses; operations and repositories
stay independent of Discord interaction mechanics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import discord

from ..config import Settings
from ..db import Database

GUILD_ONLY_MESSAGE = "This command can only be used inside an allowed Discord server."
GUILD_NOT_ALLOWED_MESSAGE = "This Discord server is not allowed to use this bridge bot."
REGISTRATION_REQUIRED_MESSAGE = "You must register with the bridge before using this command. Use `/register` first."


@dataclass(slots=True)
class GuildGuardResult:
    """Describe whether a Discord interaction passed guild deployment checks.

    `reason` is stable for command tests and logs. The guard does not write
    audit events because deployment scoping is command-context validation, not a
    management authorization decision.
    """

    allowed: bool
    guild_id: int | None
    message: str | None = None
    reason: str | None = None


def _configured_guild_allowlist(settings: Settings | object | None) -> list[str]:
    """Return the configured guild allowlist, tolerating lightweight test settings."""
    if settings is None:
        return []
    value = getattr(settings, "discord_guild_allowlist", [])
    return [str(entry) for entry in value]


def check_guild_allowed(*, settings: Settings | object | None, guild_id: int | None) -> GuildGuardResult:
    """Return whether one command may run in this Discord guild context."""
    if guild_id is None:
        return GuildGuardResult(
            allowed=False,
            guild_id=None,
            message=GUILD_ONLY_MESSAGE,
            reason="no_guild",
        )

    allowlist = _configured_guild_allowlist(settings)
    if allowlist and str(guild_id) not in allowlist:
        return GuildGuardResult(
            allowed=False,
            guild_id=guild_id,
            message=GUILD_NOT_ALLOWED_MESSAGE,
            reason="not_allowlisted",
        )

    return GuildGuardResult(allowed=True, guild_id=guild_id)


async def reject_if_guild_not_allowed(interaction: discord.Interaction, *, settings: Settings | object | None) -> bool:
    """Send an ephemeral guild-scope rejection and return True when flow stops."""
    result = check_guild_allowed(settings=settings, guild_id=getattr(interaction, "guild_id", None))
    if result.allowed:
        return False

    # Guard responses are initial interaction replies for slash commands and
    # modal submits. Keep them private because they are deployment context.
    await interaction.response.send_message(result.message or GUILD_ONLY_MESSAGE, ephemeral=True)
    return True


def check_guild_autocomplete_disallowed(interaction: discord.Interaction, settings: Settings | object | None) -> bool:
    """Return True when autocomplete should quietly show no choices.

    Autocomplete interactions cannot display normal ephemeral rejection messages.
    Returning no choices prevents network/database discovery for disallowed guilds
    while keeping Discord's autocomplete surface stable.
    """
    return not check_guild_allowed(
        settings=settings,
        guild_id=getattr(interaction, "guild_id", None),
    ).allowed


def is_registered_discord_user(*, database: Database | Any, discord_user_id: str) -> bool:
    """Return whether a Discord user has completed bridge registration."""
    return database.users.get_user_by_discord_user_id(discord_user_id) is not None


async def reject_if_user_not_registered(interaction: discord.Interaction, *, database: Database | Any) -> bool:
    """Send registration guidance and return True when command flow stops."""
    if is_registered_discord_user(database=database, discord_user_id=str(interaction.user.id)):
        return False

    # Registration is an onboarding prerequisite, not a management forbidden
    # decision. It should not create audit rows or run domain operations.
    await interaction.response.send_message(REGISTRATION_REQUIRED_MESSAGE, ephemeral=True)
    return True
