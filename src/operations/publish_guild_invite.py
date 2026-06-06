"""DiscordOps operation for publishing one guild invite on the dashboard.

The operation selects an invite-capable host channel from the guild's active
local communities. Callers do not choose a channel; they only request that the
guild publish an invite.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import discord
from discordops import OperationDefinition, OperationResult, Precondition, run_operation_definition_async

from ..db import Database
from ..models import LocalCommunity
from .guild_invite_lock import guild_invite_lock

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PublishGuildInviteInput:
    """Carry the Discord and persistence state required to publish an invite."""

    database: Database
    client: discord.Client
    guild: discord.Guild
    actor_discord_user_id: str
    _active_communities: list[LocalCommunity] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _selected_channel: discord.abc.GuildChannel | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def get_active_communities(self) -> list[LocalCommunity]:
        """Return active local communities in the repository's stable order."""
        if self._active_communities is None:
            self._active_communities = (
                self.database.local_communities.list_active_local_communities_by_guild(
                    discord_guild_id=self.guild.id
                )
            )
        return self._active_communities

    def get_selected_channel(self) -> discord.abc.GuildChannel | None:
        """Return the first active host channel where the bot can create an invite."""
        if self._selected_channel is not None:
            return self._selected_channel

        # The repository order makes automatic selection deterministic. Missing
        # cache entries and channels without invite support are simply skipped.
        for community in self.get_active_communities():
            channel = self.guild.get_channel(int(community.discord_forum_channel_id))
            if channel is None or not callable(getattr(channel, "create_invite", None)):
                continue
            permissions = channel.permissions_for(self.guild.me)
            if permissions.create_instant_invite:
                self._selected_channel = channel
                return channel
        return None


def _reject(
    operation_input: PublishGuildInviteInput,
    *,
    reason: str,
    message: str,
    **_: object,
) -> OperationResult:
    """Return one rejected result for the command adapter."""
    return OperationResult(applied=False, reason=reason, message=message)


def _active_local_community_exists(operation_input: PublishGuildInviteInput) -> bool:
    """Return whether the guild has at least one active local community."""
    return bool(operation_input.get_active_communities())


def _invitable_host_channel_exists(operation_input: PublishGuildInviteInput) -> bool:
    """Return whether one active host channel can create a Discord invite."""
    return operation_input.get_selected_channel() is not None


async def _delete_invite_best_effort(
    invite: discord.Invite,
    *,
    reason: str,
    log_message: str,
) -> None:
    """Delete one Discord invite without changing the primary operation result."""
    try:
        await invite.delete(reason=reason)
    except discord.NotFound:
        return
    except Exception:
        logger.exception(log_message)


async def _body(operation_input: PublishGuildInviteInput) -> OperationResult:
    """Create, persist, and publish an invite for the selected host channel."""
    channel = operation_input.get_selected_channel()
    assert channel is not None, "publish body requires an invite-capable host channel"

    try:
        invite = await channel.create_invite(
            max_age=0,
            max_uses=0,
            unique=True,
            reason="Published on bridge dashboard",
        )
    except Exception:
        logger.exception("Failed to create Discord guild invite")
        return OperationResult(
            applied=False,
            reason="create_invite_failed",
            message="Discord could not create the invite.",
        )

    previous = operation_input.database.guild_invite_publications.get_by_guild_id(
        operation_input.guild.id
    )
    try:
        operation_input.database.management_actions.replace_guild_invite_publication(
            discord_guild_id=operation_input.guild.id,
            discord_channel_id=channel.id,
            invite_code=str(invite.code),
            invite_url=str(invite.url),
            actor_discord_user_id=operation_input.actor_discord_user_id,
        )
    except Exception:
        logger.exception("Failed to persist Discord guild invite publication")
        await _delete_invite_best_effort(
            invite,
            reason="Bridge publication persistence failed",
            log_message="Failed to compensate newly created Discord invite",
        )
        return OperationResult(
            applied=False,
            reason="persistence_failed",
            message="The invite was not published because the bridge could not save it.",
        )

    if previous is not None:
        try:
            old_invite = await operation_input.client.fetch_invite(previous.invite_code)
        except discord.NotFound:
            old_invite = None
        except Exception:
            logger.exception("Failed to fetch replaced Discord invite %s", previous.invite_code)
            old_invite = None
        if old_invite is not None:
            await _delete_invite_best_effort(
                old_invite,
                reason="Replaced bridge dashboard invite",
                log_message=f"Failed to delete replaced Discord invite {previous.invite_code}",
            )

    outcome = "replaced" if previous is not None else "published"
    return OperationResult(
        applied=True,
        reason=outcome,
        message=f"Published invite: {invite.url}",
        extra_kwargs={"invite_url": str(invite.url), "channel": channel},
    )


publish_guild_invite_operation = OperationDefinition(
    name="publish_guild_invite",
    preconditions=(
        Precondition(
            name="no_active_local_community",
            message="This server has no active local community.",
            predicate=_active_local_community_exists,
        ),
        Precondition(
            name="no_invitable_local_community_channel",
            message="The bot cannot create an invite in any active local-community channel.",
            predicate=_invitable_host_channel_exists,
        ),
    ),
    reject=_reject,
    body=_body,
)


async def run_publish_guild_invite(operation_input: PublishGuildInviteInput) -> OperationResult:
    """Serialize and execute one guild invite publication operation."""
    async with guild_invite_lock(operation_input.guild.id):
        return await run_operation_definition_async(publish_guild_invite_operation, operation_input)
