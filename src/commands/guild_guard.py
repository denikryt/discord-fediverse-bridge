"""Discord presentation adapter for shared command-access policies."""

from __future__ import annotations

import inspect
from typing import Any

import discord
from discordops import PolicyDefinition, PolicyResult, evaluate_policy_async

from ..config import Settings
from ..db import Database
from ..operations.common_preconditions import (
    GUILD_COMMAND_ACCESS,
    GUILD_NOT_ALLOWED_MESSAGE,
    GUILD_ONLY_MESSAGE,
    REGISTERED_GUILD_COMMAND_ACCESS,
    REGISTRATION_REQUIRED_MESSAGE,
    CommandAccessInput,
)


async def evaluate_command_access(
    interaction: discord.Interaction,
    *,
    definition: PolicyDefinition,
    settings: Settings,
    database: Database | Any | None = None,
) -> PolicyResult:
    """Evaluate one access policy from primitive interaction identity fields."""
    policy_input = CommandAccessInput(
        settings=settings,
        database=database,
        discord_guild_id=getattr(interaction, "guild_id", None),
        discord_user_id=str(interaction.user.id),
    )
    return await evaluate_policy_async(definition, policy_input)


async def send_command_access_rejection(
    interaction: discord.Interaction,
    result: PolicyResult,
) -> None:
    """Send a private policy rejection using initial response or follow-up safely."""
    message = result.message or GUILD_ONLY_MESSAGE
    response = interaction.response
    # Modal and command error paths may already have acknowledged the interaction;
    # follow-up preserves the policy result without triggering double-response errors.
    is_done = getattr(response, "is_done", None)
    done = is_done() if callable(is_done) else False
    # Lightweight AsyncMock interactions may model every attribute as async,
    # while discord.py exposes ``is_done`` synchronously. Treat such test-only
    # awaitables as not acknowledged and close raw coroutines to avoid warnings.
    if inspect.isawaitable(done):
        if inspect.iscoroutine(done):
            done.close()
        done = False
    if done:
        await interaction.followup.send(message, ephemeral=True)
        return
    await response.send_message(message, ephemeral=True)


async def reject_if_command_access_denied(
    interaction: discord.Interaction,
    *,
    definition: PolicyDefinition,
    settings: Settings,
    database: Database | Any | None = None,
) -> bool:
    """Evaluate a policy, present a denial, and report whether handler flow stops."""
    result = await evaluate_command_access(
        interaction,
        definition=definition,
        settings=settings,
        database=database,
    )
    if result.allowed:
        return False
    await send_command_access_rejection(interaction, result)
    return True


async def command_access_allows_autocomplete(
    interaction: discord.Interaction,
    *,
    definition: PolicyDefinition,
    settings: Settings,
    database: Database | Any | None = None,
) -> bool:
    """Evaluate policy quietly for autocomplete, which cannot send normal replies."""
    result = await evaluate_command_access(
        interaction,
        definition=definition,
        settings=settings,
        database=database,
    )
    return result.allowed


__all__ = [
    "GUILD_COMMAND_ACCESS",
    "REGISTERED_GUILD_COMMAND_ACCESS",
    "GUILD_ONLY_MESSAGE",
    "GUILD_NOT_ALLOWED_MESSAGE",
    "REGISTRATION_REQUIRED_MESSAGE",
    "evaluate_command_access",
    "send_command_access_rejection",
    "reject_if_command_access_denied",
    "command_access_allows_autocomplete",
]
