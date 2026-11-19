"""User-owned remote Person subscriptions through existing DiscordOps policy gates.

Only upper-level roots of remote persons are rendered through webhooks; these
commands manage authorization and channel assignments, never Discord delivery.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from sqlalchemy import select

from ..bridge_policy import BridgePolicyService
from ..config import Settings
from ..db import Database
from ..models import RemotePersonChannelAssignment, RemotePersonFollow, User
from ..remote_person_feeds import RemotePersonChannelService
from ..remote_person_follow_service import RemotePersonFollowService
from .account_settings import _run_owned_sync_mutation
from .guild_guard import REGISTERED_GUILD_COMMAND_ACCESS, reject_if_command_access_denied

logger = logging.getLogger(__name__)


def _target_permissions(interaction: discord.Interaction, channel: discord.TextChannel) -> object | None:
    """Return effective bot permissions for this guild's text channel only."""
    guild = interaction.guild
    member = None if guild is None else guild.me
    if (guild is None or member is None or channel.guild.id != guild.id
            or channel.type != discord.ChannelType.text):
        return None
    return channel.permissions_for(member)


def _rows_for_owner(database: Database, discord_user_id: str, guild_id: int):
    """Read only the caller's own Follow generations and assignments in this guild."""
    with database.session() as session:
        rows = session.execute(select(
            RemotePersonFollow.remote_actor_id,
            RemotePersonFollow.relationship_status,
            RemotePersonFollow.follow_dispatch_state,
            RemotePersonChannelAssignment.discord_channel_id,
        ).join(RemotePersonChannelAssignment, RemotePersonChannelAssignment.follow_id == RemotePersonFollow.id)
         .join(User, User.id == RemotePersonFollow.user_id)
         .where(User.discord_user_id == discord_user_id,
                RemotePersonChannelAssignment.discord_guild_id == guild_id,
                RemotePersonFollow.relationship_status.in_(("pending", "accepted", "rejected", "cancel_requested")))
         .order_by(RemotePersonFollow.remote_actor_id, RemotePersonChannelAssignment.discord_channel_id)).all()
        return list(rows)


def register(
    tree: app_commands.CommandTree,
    database: Database,
    settings: Settings,
    policy_service: BridgePolicyService,
    follow_service: RemotePersonFollowService,
) -> None:
    """Register guild-only Person Feed commands with the existing registered policy."""
    channel_service = RemotePersonChannelService(database)
    group = app_commands.Group(name="person-feed", description="Manage your remote Fediverse people")

    async def allowed(interaction: discord.Interaction) -> bool:
        """Use identical ban, guild policy and registration order as existing commands."""
        return not await reject_if_command_access_denied(
            interaction, definition=REGISTERED_GUILD_COMMAND_ACCESS,
            settings=settings, database=database, policy_service=policy_service,
        )

    async def manager_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> object | None:
        """Reject nonmanager or unusable channels before lookup and SQL writes."""
        if not getattr(getattr(interaction.user, 'guild_permissions', None), 'manage_guild', False):
            await interaction.response.send_message('Manage Server is required for channel assignments.', ephemeral=True)
            return None
        flags = _target_permissions(interaction, channel)
        if flags is None:
            await interaction.response.send_message('Choose a text channel in this server.', ephemeral=True)
            return None
        return flags

    @group.command(name='follow', description='Follow a remote Person into a text channel')
    @app_commands.describe(actor='Fediverse handle or canonical Actor URL', channel='Text channel for new posts')
    async def follow(interaction: discord.Interaction, actor: str, channel: discord.TextChannel) -> None:
        """Reserve the caller's signed Follow without changing any other owner."""
        if not await allowed(interaction):
            return
        flags = await manager_channel(interaction, channel)
        if flags is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await follow_service.follow(
                discord_user_id=str(interaction.user.id), actor=actor,
                discord_guild_id=interaction.guild_id, discord_channel_id=channel.id,
                target_channel_guild_id=channel.guild.id, channel_type='text',
                caller_can_manage_guild=True, bot_permissions=flags,
            )
        except Exception:
            logger.exception('Person follow resolution failed')
            await interaction.edit_original_response(content='Could not verify the remote Person. No Follow was sent.')
            return
        # The durable reservation exists before transport; a refused network
        # attempt never requires a replacement Follow identity.
        if (result.status in ('prepared', 'already_assigned')
                and result.follow_id is not None and result.follow_activity_id is not None):
            try:
                # Repeat the caller's still-prepared Follow, not whichever
                # unrelated prepared row happens to lead the global queue.
                await follow_service.dispatch_prepared_for(
                    follow_id=result.follow_id, follow_activity_id=result.follow_activity_id)
            except Exception:
                logger.exception('Immediate Person Follow dispatch failed; reservation retained')
        status = {
            'prepared': 'Follow reserved; awaiting remote acceptance.',
            'assigned_existing': 'Channel assigned to your existing Follow.',
            'already_assigned': 'You already assigned this Person to that channel.',
            'missing_bot_permissions': 'The bot lacks channel permissions: ' + ', '.join(result.missing_permissions),
            'invalid_actor': 'Remote Person or inbox is invalid.',
            'federation_denied': 'Federation policy does not permit this Person or their inbox.',
            'guild_denied': 'This server is not permitted by bridge policy.',
            'channel_conflict': 'Channel belongs to another bridge mode.',
            'guild_assignment_conflict': 'This Follow already has a destination in this server; remove that assignment first.',
        }.get(result.status, f'Follow was not created: {result.status}.')
        await interaction.edit_original_response(content=status)

    @group.command(name='add-channel', description='Add a destination for your existing Follow')
    @app_commands.describe(actor='Fediverse handle or canonical Actor URL')
    async def add_channel(interaction: discord.Interaction, actor: str, channel: discord.TextChannel) -> None:
        """Share a text channel while isolating local users' Follow ownership."""
        if not await allowed(interaction):
            return
        flags = await manager_channel(interaction, channel)
        if flags is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        canonical = await follow_service.canonical_owned_actor(
            discord_user_id=str(interaction.user.id), identifier=actor,
            require_admission_policy=True,
        )
        if canonical is None:
            await interaction.edit_original_response(content='No active permitted Follow for that Actor.')
            return
        result = await _run_owned_sync_mutation(lambda: channel_service.add_channel(
            discord_user_id=str(interaction.user.id), remote_actor_id=canonical,
            discord_guild_id=interaction.guild_id, discord_channel_id=channel.id,
            target_channel_guild_id=channel.guild.id, channel_type='text',
            caller_can_manage_guild=True, bot_permissions=flags,
        ))
        await interaction.edit_original_response(content={
            'assigned': 'Destination added.',
            'already_assigned': 'That destination is already assigned.',
            'missing_bot_permissions': 'The bot lacks channel permissions: ' + ', '.join(result.missing_permissions),
            'channel_conflict': 'Channel belongs to another bridge mode.',
            'guild_assignment_conflict': 'This Follow already has a destination in this server; remove that assignment first.',
            'not_following': 'You have no active Follow for that exact Actor ID.',
        }.get(result.status, f'Assignment not changed: {result.status}.'))

    @group.command(name='remove-channel', description='Remove one of your own destinations')
    @app_commands.describe(actor='Fediverse handle or canonical Actor URL')
    async def remove_channel(interaction: discord.Interaction, actor: str, channel: discord.TextChannel) -> None:
        """Remove only the caller's channel association, never another owner's."""
        if not await allowed(interaction):
            return
        if interaction.guild is None or channel.guild.id != interaction.guild_id:
            await interaction.response.send_message('Choose a channel from this server.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        canonical = await follow_service.canonical_owned_actor(
            discord_user_id=str(interaction.user.id), identifier=actor)
        if canonical is None:
            await interaction.edit_original_response(content='You do not have that destination.')
            return
        result = await _run_owned_sync_mutation(lambda: channel_service.remove_channel(
            discord_user_id=str(interaction.user.id), remote_actor_id=canonical,
            discord_guild_id=interaction.guild_id, discord_channel_id=channel.id,
        ))
        if result.status == 'removed':
            try:
                await follow_service.dispatch_cancellations(limit=1)
            except Exception:
                logger.exception('Immediate Person Undo dispatch failed; cancellation retained')
        await interaction.edit_original_response(content={
            'removed': 'Your destination removed. Last assignment schedules your own Undo.',
            'not_assigned': 'You do not have that destination.',
        }.get(result.status, f'Destination not removed: {result.status}.'))

    @group.command(name='unfollow', description='Remove all your destinations for a remote Person')
    @app_commands.describe(actor='Fediverse handle or canonical Actor URL')
    async def unfollow(interaction: discord.Interaction, actor: str) -> None:
        """Cancel the caller's Follow without revoking any other user's generation."""
        if not await allowed(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        canonical = await follow_service.canonical_owned_actor(
            discord_user_id=str(interaction.user.id), identifier=actor)
        if canonical is None:
            await interaction.edit_original_response(content='No active Follow for that Actor.')
            return
        result = await _run_owned_sync_mutation(lambda: follow_service.cancel_follow(
            discord_user_id=str(interaction.user.id), remote_actor_id=canonical,
        ))
        if result == 'cancel_requested':
            try:
                await follow_service.dispatch_cancellations(limit=1)
            except Exception:
                logger.exception('Immediate Person Undo dispatch failed; cancellation retained')
        await interaction.edit_original_response(content=(
            'Your Follow has been cancelled or scheduled for Undo.'
            if result == 'cancel_requested' else 'No active Follow for that Actor.'
        ))

    @group.command(name='list', description='Show your Follow destinations in this server')
    async def list_follows(interaction: discord.Interaction) -> None:
        """Expose no other local user's subscriber or Follow state."""
        if not await allowed(interaction):
            return
        rows = await asyncio.to_thread(_rows_for_owner, database, str(interaction.user.id), interaction.guild_id)
        details = '\n'.join(
            f'`{actor}` — {status} (transport: {dispatch}), <#{channel}>'
            for actor, status, dispatch, channel in rows
        )
        await interaction.response.send_message((details[:1850] if details else 'You have no Person Feed destinations here.'), ephemeral=True)

    tree.add_command(group)
