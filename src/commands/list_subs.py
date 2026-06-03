from __future__ import annotations

import discord
from discord import app_commands
from discordops import run_operation_definition_async
from ..community_labels import community_relay_label
from ..db import Database
from ..operations import ListSubscriptionsInput, list_subscriptions_operation


def register(tree: app_commands.CommandTree, database: Database) -> None:
    # The registered slash command delegates empty-state policy to the
    # operation layer and keeps Discord embed rendering in the adapter.
    @tree.command(name="list-subscriptions", description="List all active channel-community subscriptions")
    async def list_subscriptions(interaction: discord.Interaction) -> None:
        # The operation determines whether the list is empty; the command keeps
        # ownership of Discord embed rendering for successful responses.
        result = await run_operation_definition_async(
            list_subscriptions_operation,
            ListSubscriptionsInput(database=database, guild_id=interaction.guild_id),
        )
        if not result.applied:
            await interaction.response.send_message(result.message, ephemeral=True)
            return

        remote_subscriptions = result.extra_kwargs["remote_subscriptions"] if result.extra_kwargs is not None else []
        local_subscribers = result.extra_kwargs["local_subscribers"] if result.extra_kwargs is not None else []
        lines: list[str] = []
        if remote_subscriptions:
            lines.append("Remote community subscriptions")
            for sub in remote_subscriptions:
                # Use channel mention so Discord renders it as a clickable link.
                channel_mention = f"<#{sub.discord_channel_id}>"
                community_label = community_relay_label(
                    actor_id=getattr(sub, "lemmy_community_actor_id", None),
                    name=getattr(sub, "lemmy_community_name", None),
                    handle=getattr(sub, "community_handle", None),
                )
                lines.append(f"• {channel_mention} → **{community_label}**")
        if local_subscribers:
            if lines:
                lines.append("")
            lines.append("Local community subscribers")
            for sub in local_subscribers:
                # The list command can safely resolve the local community label
                # server-side because it is a moderator-only surface.
                channel_mention = f"<#{sub.discord_channel_id}>"
                local_community = database.local_communities.get_local_community_by_id(sub.local_community_id)
                if local_community is not None:
                    community_label = community_relay_label(
                        actor_id=getattr(local_community, "actor_url", None),
                        name=getattr(local_community, "slug", None),
                    )
                else:
                    community_label = f"local community #{sub.local_community_id}"
                lines.append(f"• {channel_mention} → **{community_label}**")

        embed = discord.Embed(
            title="Active Subscriptions",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{len(remote_subscriptions) + len(local_subscribers)} subscription(s)")
        # ephemeral=True keeps the output visible only to the invoking user to
        # avoid cluttering the channel with a potentially long list.
        await interaction.response.send_message(embed=embed, ephemeral=True)
