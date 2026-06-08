"""DiscordOps operation for removing a published guild invite."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import discord
from discordops import OperationDefinition, OperationResult, Precondition, run_operation_definition_async

from ..db import Database
from ..models import GuildInvitePublication
from .guild_invite_lock import guild_invite_lock

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RemoveGuildInviteInput:
    """Carry the Discord and persistence state required to remove an invite."""

    database: Database
    client: discord.Client
    guild: discord.Guild
    actor_discord_user_id: str
    _current_publication: GuildInvitePublication | None = field(default=None, init=False, repr=False)
    _publication_loaded: bool = field(default=False, init=False, repr=False)

    def get_current_publication(self) -> GuildInvitePublication | None:
        """Load and memoize the current guild invite publication."""
        if not self._publication_loaded:
            self._current_publication = (
                self.database.guild_invite_publications.get_by_guild_id(self.guild.id)
            )
            self._publication_loaded = True
        return self._current_publication


def _reject(
    operation_input: RemoveGuildInviteInput,
    *,
    reason: str,
    message: str,
    **_: object,
) -> OperationResult:
    """Return one rejected result for the command adapter."""
    return OperationResult(applied=False, reason=reason, message=message)


def _has_published_guild_invite(operation_input: RemoveGuildInviteInput) -> bool:
    """Return whether this guild currently has a published invite."""
    return operation_input.get_current_publication() is not None


async def _body(operation_input: RemoveGuildInviteInput) -> OperationResult:
    """Delete the Discord invite and then clear its dashboard publication."""
    current = operation_input.get_current_publication()
    assert current is not None

    try:
        invite = await operation_input.client.fetch_invite(current.invite_code)
        await invite.delete(reason="Removed from bridge dashboard")
    except discord.NotFound:
        # Manual deletion already satisfies the Discord-side outcome.
        pass
    except Exception:
        logger.exception("Failed to delete published Discord invite")
        return OperationResult(
            applied=False,
            reason="delete_invite_failed",
            message="Discord could not delete the published invite; the dashboard link was kept.",
        )

    try:
        operation_input.database.management_actions.remove_guild_invite_publication(
            discord_guild_id=operation_input.guild.id,
            actor_discord_user_id=operation_input.actor_discord_user_id,
        )
    except Exception:
        logger.exception("Failed to remove persisted guild invite publication")
        return OperationResult(
            applied=False,
            reason="persistence_failed",
            message="The Discord invite was deleted, but the bridge could not clear the dashboard state.",
        )

    return OperationResult(
        applied=True,
        reason="removed",
        message="Removed the published server invite.",
    )


remove_guild_invite_operation = OperationDefinition(
    name="remove_guild_invite",
    preconditions=(
        Precondition(
            name="guild_invite_not_published",
            message="No server invite is currently published.",
            predicate=_has_published_guild_invite,
        ),
    ),
    reject=_reject,
    body=_body,
)


async def run_remove_guild_invite(operation_input: RemoveGuildInviteInput) -> OperationResult:
    """Serialize and execute one guild invite removal operation."""
    async with guild_invite_lock(operation_input.guild.id):
        return await run_operation_definition_async(remove_guild_invite_operation, operation_input)
