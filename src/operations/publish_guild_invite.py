"""DiscordOps operation for publishing one guild invite on the dashboard.

The operation declares all eligibility rules as ordered preconditions. Its body
only performs Discord and persistence side effects after those rules pass.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
import discord
from discordops import OperationDefinition, OperationResult, Precondition, run_operation_definition_async

from ..db import Database
from .guild_invite_lock import guild_invite_lock

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PublishGuildInviteInput:
    """Carry the Discord and persistence state required to publish an invite."""

    database: Database
    client: discord.Client
    guild: discord.Guild
    channel: discord.abc.GuildChannel
    actor_discord_user_id: str
    _active_host_channel_ids: set[int] | None = field(default=None, init=False, repr=False)

    def get_active_host_channel_ids(self) -> set[int]:
        """Load and memoize active local-community host channels for the guild."""
        # Several preconditions use the same query, so one operation execution
        # must observe one consistent set without duplicate DB reads.
        if self._active_host_channel_ids is None:
            rows = self.database.local_communities.list_active_local_communities_by_guild(
                discord_guild_id=self.guild.id
            )
            self._active_host_channel_ids = {int(row.discord_forum_channel_id) for row in rows}
        return self._active_host_channel_ids


def _reject(
    operation_input: PublishGuildInviteInput,
    *,
    reason: str,
    message: str,
    **_: object,
) -> OperationResult:
    """Return one rejected result for the command adapter."""
    return OperationResult(applied=False, reason=reason, message=message)


def _channel_belongs_to_guild(operation_input: PublishGuildInviteInput) -> bool:
    """Return whether the selected channel belongs to the interaction guild."""
    return operation_input.channel.guild.id == operation_input.guild.id


def _active_local_community_exists(operation_input: PublishGuildInviteInput) -> bool:
    """Return whether the guild has at least one active local community."""
    return bool(operation_input.get_active_host_channel_ids())


def _channel_hosts_active_local_community(operation_input: PublishGuildInviteInput) -> bool:
    """Return whether the selected channel hosts an active local community."""
    return operation_input.channel.id in operation_input.get_active_host_channel_ids()


def _channel_is_public(operation_input: PublishGuildInviteInput) -> bool:
    """Return whether everyone can view the selected channel."""
    permissions = operation_input.channel.permissions_for(operation_input.guild.default_role)
    return permissions.view_channel


def _bot_can_create_invite(operation_input: PublishGuildInviteInput) -> bool:
    """Return whether the bot may create an invite in the selected channel."""
    permissions = operation_input.channel.permissions_for(operation_input.guild.me)
    return permissions.create_instant_invite


def _channel_supports_invites(operation_input: PublishGuildInviteInput) -> bool:
    """Return whether the Discord channel exposes invite creation."""
    return callable(operation_input.channel.create_invite)


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
        # A missing invite already satisfies cleanup.
        return
    except Exception:
        logger.exception(log_message)


async def _body(operation_input: PublishGuildInviteInput) -> OperationResult:
    """Create, persist, and publish a valid guild invite.

    Persistence is authoritative for dashboard publication. A failed write
    compensates by deleting the newly created Discord invite. Cleanup of the
    previous invite happens only after the new publication commits and remains
    best-effort so a cleanup failure cannot roll back a working replacement.
    """
    try:
        invite = await operation_input.channel.create_invite(
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
            discord_channel_id=operation_input.channel.id,
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
        extra_kwargs={"invite_url": str(invite.url)},
    )


publish_guild_invite_operation = OperationDefinition(
    name="publish_guild_invite",
    preconditions=(
        Precondition(
            name="channel_not_in_guild",
            message="The selected channel does not belong to this server.",
            predicate=_channel_belongs_to_guild,
        ),
        Precondition(
            name="no_active_local_community",
            message="This server has no active local community.",
            predicate=_active_local_community_exists,
        ),
        Precondition(
            name="channel_not_active_local_community_host",
            message="Select a channel that hosts an active local community.",
            predicate=_channel_hosts_active_local_community,
        ),
        Precondition(
            name="private_channel",
            message="The selected channel must be visible to everyone.",
            predicate=_channel_is_public,
        ),
        Precondition(
            name="bot_permission_missing",
            message="The bot needs Create Instant Invite in the selected channel.",
            predicate=_bot_can_create_invite,
        ),
        Precondition(
            name="channel_invites_unsupported",
            message="The selected channel does not support Discord invites.",
            predicate=_channel_supports_invites,
        ),
    ),
    reject=_reject,
    body=_body,
)


async def run_publish_guild_invite(operation_input: PublishGuildInviteInput) -> OperationResult:
    """Serialize and execute one guild invite publication operation."""
    async with guild_invite_lock(operation_input.guild.id):
        return await run_operation_definition_async(publish_guild_invite_operation, operation_input)
