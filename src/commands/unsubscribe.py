from __future__ import annotations

import logging

import discord
from discord import app_commands
from discordops import run_operation_definition_async

from ..community_labels import community_relay_label
from ..db import Database
from ..bridge_policy import BridgePolicyService
from ..config import Settings
from ..fedify_gateway_client import FedifyGatewayClient
from .guild_guard import REGISTERED_GUILD_COMMAND_ACCESS, reject_if_command_access_denied
from ..operations import UnsubscribeInput, unsubscribe_operation
from ..operations.unsubscribe_local_community import (
    UnsubscribeLocalCommunityInput,
    unsubscribe_local_community_operation,
)

logger = logging.getLogger(__name__)


def _no_ping_allowed_mentions() -> discord.AllowedMentions:
    """Suppress notifications for display-only user mentions in command output."""
    # Discord still renders <@user_id> as a clickable user reference, while the
    # allowed_mentions object prevents the response from notifying that user.
    return discord.AllowedMentions.none()


def _user_mention(user_id: object) -> str:
    """Return Discord mention markup for the moderator who invoked the command."""
    return f"<@{user_id}>"


def _remote_subscription_label(subscription: object) -> str:
    """Return the compact remote community label stored on a subscription row."""
    return community_relay_label(
        actor_id=getattr(subscription, "lemmy_community_actor_id", None),
        name=getattr(subscription, "lemmy_community_name", None),
        handle=getattr(subscription, "community_handle", None),
    )


def _local_subscriber_label(database: Database, local_subscriber: object) -> str:
    """Return a compact label for the local community targeted by a subscriber row."""
    local_community = database.local_communities.get_local_community_by_id(
        getattr(local_subscriber, "local_community_id")
    )
    return community_relay_label(
        actor_id=getattr(local_community, "actor_url", None),
        name=getattr(local_community, "slug", None),
    )


def _unsubscribe_success_message(*, user_id: object, channel_mention: str, community_label: str) -> str:
    """Build the public success message for /unsubscribe-channel."""
    return f"{_user_mention(user_id)} unsubscribed {channel_mention} from **{community_label}**."


def _remote_unfollow_failure_message(
    *,
    user_id: object,
    channel_mention: str,
    community_label: str,
    operation_message: str,
) -> str:
    """Preserve retry diagnostics while replacing the old community label prefix."""
    marker = " locally, but remote Undo(Follow) failed: "
    if marker not in operation_message:
        return operation_message
    error_detail = operation_message.split(marker, 1)[1]
    return (
        f"{_user_mention(user_id)} unsubscribed {channel_mention} from "
        f"**{community_label}** locally, but remote Undo(Follow) failed: {error_detail}"
    )


def register(
    tree: app_commands.CommandTree,
    database: Database,
    fedify_gateway: FedifyGatewayClient,
    settings: Settings,
    policy_service: BridgePolicyService | None = None,
) -> None:
    """Register the unsubscribe-channel slash command on the given command tree."""
    policy_service = policy_service or BridgePolicyService(settings=settings, repository=database.bridge_policy_entries)
    # The registered slash command adapts Discord input into the operation
    # contract and leaves policy decisions to the framework-backed layer.
    @tree.command(name="unsubscribe-channel", description="Remove a forum channel's Lemmy subscription")
    @app_commands.describe(channel="Forum channel to unsubscribe")
    @app_commands.default_permissions(manage_channels=True)
    async def unsubscribe_channel(
        interaction: discord.Interaction,
        channel: discord.ForumChannel,
    ) -> None:
        """Handle the /unsubscribe-channel slash command."""
        if await reject_if_command_access_denied(interaction, definition=REGISTERED_GUILD_COMMAND_ACCESS, settings=settings, database=database, policy_service=policy_service):
            return
        # The command adapter only supplies Discord-facing context; the
        # operation decides whether deletion is allowed and what result to show.
        remote_subscription = database.remote_subscriptions.get_subscription_by_channel(channel.id)
        local_subscriber = database.local_subscribers.get_local_subscriber_by_channel(channel.id)
        community_label: str | None = None
        if remote_subscription is not None:
            community_label = _remote_subscription_label(remote_subscription)
            result = await run_operation_definition_async(
                unsubscribe_operation,
                UnsubscribeInput(
                    database=database,
                    fedify_gateway=fedify_gateway,
                    channel_id=channel.id,
                    channel_mention=channel.mention,
                    policy_service=policy_service,
                ),
            )
        elif local_subscriber is not None:
            community_label = _local_subscriber_label(database, local_subscriber)
            result = await run_operation_definition_async(
                unsubscribe_local_community_operation,
                UnsubscribeLocalCommunityInput(
                    database=database,
                    channel_id=channel.id,
                    channel_mention=channel.mention,
                ),
            )
        else:
            await interaction.response.send_message(
                f"Channel {channel.mention} has no active subscription.",
                ephemeral=True,
            )
            return
        message = result.message
        send_kwargs: dict[str, object] = {"ephemeral": not result.applied}
        if result.applied and community_label is not None:
            message = _unsubscribe_success_message(
                user_id=interaction.user.id,
                channel_mention=channel.mention,
                community_label=community_label,
            )
            send_kwargs["allowed_mentions"] = _no_ping_allowed_mentions()
        elif getattr(result, "reason", None) == "remote_unfollow_failed" and community_label is not None:
            message = _remote_unfollow_failure_message(
                user_id=interaction.user.id,
                channel_mention=channel.mention,
                community_label=community_label,
                operation_message=result.message,
            )
            send_kwargs["allowed_mentions"] = _no_ping_allowed_mentions()

        await interaction.response.send_message(message, **send_kwargs)
        if result.applied:
            logger.info("Unsubscribed channel %s", channel.id)
