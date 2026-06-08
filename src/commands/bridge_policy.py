"""Discord slash-command group for private dynamic bridge policy management."""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

import discord
from discord import app_commands

from ..bridge_policy import BridgePolicyService, PolicyType
from ..db import Database
from ..operations import (
    ListBridgePolicyInput,
    ManageBridgePolicyInput,
    list_bridge_policy_operation,
    manage_bridge_policy_operation,
)
from ..user_bans import UserBanService

logger = logging.getLogger(__name__)

_POLICY_CHOICES = [
    app_commands.Choice(name="Federation allowlist", value="federation-allow"),
    app_commands.Choice(name="Federation blocklist", value="federation-block"),
    app_commands.Choice(name="Discord guild allowlist", value="guild-allow"),
    app_commands.Choice(name="Discord guild blocklist", value="guild-block"),
    app_commands.Choice(name="Bridge super-admins", value="super-admin"),
]


def _private_kwargs(interaction: discord.Interaction) -> dict[str, bool]:
    """Use ephemeral responses in guilds and ordinary private responses in DMs."""
    return {"ephemeral": interaction.guild_id is not None}


def _matches(value: str, current: str) -> bool:
    """Apply case-insensitive Discord autocomplete filtering."""
    return current.casefold() in value.casefold()


def _known_remote_hosts(database: Database) -> list[str]:
    """Collect canonical hosts already known through persisted federation state."""
    values: set[str] = set()
    for row in database.remote_subscriptions.list_subscriptions():
        raw = str(getattr(row, "lemmy_community_actor_id", "") or getattr(row, "community_actor_id", "") or getattr(row, "community_url", ""))
        host = urlsplit(raw).hostname
        if host:
            values.add(host.lower())
    for row in database.bridge_actor_follows.list_bridge_actor_follows():
        host = urlsplit(str(getattr(row, "community_actor_id", ""))).hostname
        if host:
            values.add(host.lower())
    for row in database.remote_subscribers.list_remote_subscribers_for_all():
        host = urlsplit(str(getattr(row, "remote_actor_id", ""))).hostname
        if host:
            values.add(host.lower())
    return sorted(values)


def _subject_autocomplete(database: Database, policy_service: BridgePolicyService, ban_service: UserBanService, bot: discord.Client, *, removing: bool):
    """Build context-sensitive subject autocomplete with private authorization."""

    async def autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """Return at most 25 choices after current policy authorization."""
        try:
            snapshot = policy_service.snapshot()
            if ban_service.check_global_discord_user(str(interaction.user.id)).banned:
                return []
            if not snapshot.is_discord_guild_allowed(interaction.guild_id):
                return []
            if not snapshot.is_super_admin(str(interaction.user.id)):
                return []
            selected = str(getattr(getattr(interaction, "namespace", None), "type", ""))
            mapping = {
                "federation-allow": PolicyType.FEDERATION_ALLOW,
                "federation-block": PolicyType.FEDERATION_BLOCK,
                "guild-allow": PolicyType.DISCORD_GUILD_ALLOW,
                "guild-block": PolicyType.DISCORD_GUILD_BLOCK,
                "super-admin": PolicyType.BRIDGE_SUPER_ADMIN,
            }
            policy_type = mapping.get(selected)
            if policy_type is None:
                return []
            if removing:
                rows = database.bridge_policy_entries.list_active_by_type(policy_type=policy_type.value)
                candidates = [(str(row.normalized_subject), str(row.normalized_subject)) for row in rows]
            elif policy_type in {PolicyType.DISCORD_GUILD_ALLOW, PolicyType.DISCORD_GUILD_BLOCK}:
                candidates = [(f"{guild.name} — {guild.id}", str(guild.id)) for guild in getattr(bot, "guilds", [])]
            elif policy_type is PolicyType.BRIDGE_SUPER_ADMIN:
                users = getattr(bot, "users", [])
                candidates = [(f"{user} — {user.id}", str(user.id)) for user in users]
            else:
                candidates = [(host, host) for host in _known_remote_hosts(database)]
            return [
                app_commands.Choice(name=label[:100], value=value)
                for label, value in candidates
                if _matches(label, current) or _matches(value, current)
            ][:25]
        except Exception:
            logger.exception("Failed to autocomplete bridge policy subject")
            return []

    return autocomplete


def register(tree: app_commands.CommandTree, database: Database, policy_service: BridgePolicyService, ban_service: UserBanService) -> None:
    """Register `/bridge-policy add|remove|list` on the application tree."""
    group = app_commands.Group(
        name="bridge-policy",
        description="Manage bridge-wide runtime policy",
        allowed_contexts=app_commands.AppCommandContext(
            guild=True,
            dm_channel=True,
            private_channel=False,
        ),
    )

    @group.command(name="add", description="Add or reactivate one dynamic policy entry")
    @app_commands.choices(type=_POLICY_CHOICES)
    @app_commands.autocomplete(subject=_subject_autocomplete(database, policy_service, ban_service, tree.client, removing=False))
    async def add(
        interaction: discord.Interaction,
        type: app_commands.Choice[str],
        subject: str,
        reason: app_commands.Range[str, 0, 500] | None = None,
    ) -> None:
        """Add one dynamic entry after DiscordOps operation preconditions."""
        result = manage_bridge_policy_operation(ManageBridgePolicyInput(
            database=database,
            policy_service=policy_service,
            ban_service=ban_service,
            discord_user_id=str(interaction.user.id),
            discord_guild_id=interaction.guild_id,
            action="add",
            policy_type_value=type.value,
            subject=subject,
            reason=reason,
        ))
        await interaction.response.send_message(result.message, **_private_kwargs(interaction))

    @group.command(name="remove", description="Deactivate one dynamic policy entry")
    @app_commands.choices(type=_POLICY_CHOICES)
    @app_commands.autocomplete(subject=_subject_autocomplete(database, policy_service, ban_service, tree.client, removing=True))
    async def remove(
        interaction: discord.Interaction,
        type: app_commands.Choice[str],
        subject: str,
        reason: app_commands.Range[str, 0, 500] | None = None,
    ) -> None:
        """Remove one active dynamic entry after DiscordOps preconditions."""
        result = manage_bridge_policy_operation(ManageBridgePolicyInput(
            database=database,
            policy_service=policy_service,
            ban_service=ban_service,
            discord_user_id=str(interaction.user.id),
            discord_guild_id=interaction.guild_id,
            action="remove",
            policy_type_value=type.value,
            subject=subject,
            reason=reason,
        ))
        await interaction.response.send_message(result.message, **_private_kwargs(interaction))

    @group.command(name="list", description="List effective entries for one policy type")
    @app_commands.choices(type=_POLICY_CHOICES)
    async def list_entries(interaction: discord.Interaction, type: app_commands.Choice[str]) -> None:
        """Return private effective policy output and visible guild IDs."""
        result = list_bridge_policy_operation(ListBridgePolicyInput(
            policy_service=policy_service,
            ban_service=ban_service,
            discord_user_id=str(interaction.user.id),
            discord_guild_id=interaction.guild_id,
            policy_type_value=type.value,
        ))
        message = result.message
        if result.allowed and type.value == "guild-allow":
            snapshot = policy_service.snapshot()
            visible = [
                f"- {guild.name} — `{guild.id}` ({'allowed' if snapshot.is_discord_guild_allowed(guild.id) else 'denied'})"
                for guild in getattr(tree.client, "guilds", [])
            ]
            message += "\n\nGuilds visible to this bot:\n" + ("\n".join(visible) if visible else "- none")
        chunks = [message[index:index + 1900] for index in range(0, len(message), 1900)] or [message]
        await interaction.response.send_message(chunks[0], **_private_kwargs(interaction))
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, **_private_kwargs(interaction))

    tree.add_command(group)
