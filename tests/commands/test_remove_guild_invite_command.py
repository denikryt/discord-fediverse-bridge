"""Command registration tests for guild invite removal."""

from __future__ import annotations

import discord
from discord import app_commands

from src.commands import remove_guild_invite
from src.db import Database


def test_remove_command_declares_manage_guild_default_permissions() -> None:
    """Discord normally hides the command from members without Manage Guild."""
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    remove_guild_invite.register(tree, Database("sqlite:///:memory:"), object())
    command = tree.get_command("remove-guild-invite")
    assert command is not None
    assert command.default_permissions.manage_guild is True
    assert command.guild_only is True
