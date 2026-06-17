"""Discord account-settings UI for registered local ActivityPub users."""

from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from typing import Callable, TypeVar

import discord
from discord import app_commands

from ..bridge_policy import BridgePolicyService
from ..config import Settings
from ..db import Database
from ..user_profiles import (
    ProfileMutationResult,
    UserProfileService,
    UserProfileValidationError,
)
from ..personal_feeds import PersonalFeedService
from ..discord_channel_types import is_normal_guild_text_channel
from .guild_guard import (
    REGISTERED_GUILD_COMMAND_ACCESS,
    reject_if_command_access_denied,
)

logger = logging.getLogger(__name__)
T = TypeVar("T")


class GuildTextChannelTransformer(app_commands.Transformer):
    """Advertise and accept only Discord protocol GUILD_TEXT channels."""

    @property
    def type(self) -> discord.AppCommandOptionType:
        return discord.AppCommandOptionType.channel

    @property
    def channel_types(self) -> list[discord.ChannelType]:
        return [discord.ChannelType.text]

    async def transform(
        self, interaction: discord.Interaction, value: object
    ) -> discord.TextChannel:
        if not is_normal_guild_text_channel(value):
            raise app_commands.TransformerError(
                value, discord.AppCommandOptionType.channel, self
            )
        return value


async def _run_owned_sync_mutation(operation: Callable[[], T]) -> T:
    """Run one bounded synchronous mutation without abandoning an active worker.

    Once the worker starts, caller cancellation waits for it and retrieves its
    result or exception before cancellation is propagated. This preserves the
    synchronous service transaction as the sole durable-operation owner.
    """
    worker = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        try:
            await worker
        finally:
            raise


async def _finish_profile_mutation(
    interaction: discord.Interaction,
    service: UserProfileService,
    result: ProfileMutationResult,
    *,
    success_message: str,
) -> None:
    """Attempt immediate fanout and report local success separately from remote state."""
    try:
        summary = await service.fanout(result)
    except Exception:
        logger.exception("Immediate profile fanout failed source=%s", result.source_id)
        summary = None
    if summary is None or summary.render_pending:
        message = "Profile updated locally, but federation refresh is pending."
    elif summary.outstanding > 0:
        message = "Profile updated locally. Some remote followers may refresh later."
    else:
        message = success_message
    await interaction.edit_original_response(content=message)


def _parse_metadata_row(value: str) -> tuple[str, str] | None:
    """Parse one optional metadata input using first-line name semantics.

    A fully blank input is omitted. Any partial row is rejected so metadata
    replacement remains atomic across all four modal positions.
    """
    if not value.strip():
        return None
    lines = value.splitlines()
    name = lines[0].strip() if lines else ""
    field_value = "\n".join(lines[1:]).strip()
    if not name or not field_value:
        raise UserProfileValidationError(
            "Each metadata field requires a name on the first line and a value below it."
        )
    return name, field_value


async def _resolve_authorized_user(
    interaction: discord.Interaction,
    *,
    database: Database,
    settings: Settings,
    policy_service: BridgePolicyService,
) -> object | None:
    """Re-evaluate registered-user access and resolve the current durable user."""
    if await reject_if_command_access_denied(
        interaction,
        definition=REGISTERED_GUILD_COMMAND_ACCESS,
        settings=settings,
        database=database,
        policy_service=policy_service,
    ):
        return None
    return database.users.get_user_by_discord_user_id(str(interaction.user.id))


class ProfileTextModal(discord.ui.Modal, title="Edit account profile"):
    """Edit display name and bio without changing metadata or actor identity."""

    display_name = discord.ui.TextInput(
        label="Display name",
        required=False,
        max_length=30,
        placeholder="Handle remains unchanged",
    )
    summary = discord.ui.TextInput(
        label="Bio",
        required=False,
        max_length=500,
        style=discord.TextStyle.paragraph,
    )

    def __init__(
        self,
        database: Database,
        settings: Settings,
        policy_service: BridgePolicyService,
        service: UserProfileService,
        *,
        display_name: str = "",
        summary: str = "",
    ) -> None:
        """Bind the interaction-scoped modal to shared profile dependencies."""
        super().__init__()
        self.database = database
        self.settings = settings
        self.policy_service = policy_service
        self.service = service
        self.display_name.default = display_name
        self.summary.default = summary

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Persist scalar text after repeating the command access policy."""
        user = await _resolve_authorized_user(
            interaction,
            database=self.database,
            settings=self.settings,
            policy_service=self.policy_service,
        )
        if user is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await _run_owned_sync_mutation(
                lambda: self.service.update_identity_text(
                    user.id, str(self.display_name), str(self.summary)
                )
            )
        except UserProfileValidationError as exc:
            await interaction.edit_original_response(content=str(exc))
            return
        await _finish_profile_mutation(
            interaction,
            self.service,
            result,
            success_message="Profile text updated. Your ActivityPub handle is unchanged.",
        )


class ProfileMetadataModal(discord.ui.Modal, title="Edit profile metadata"):
    """Edit four ordered Mastodon-compatible metadata rows independently."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        policy_service: BridgePolicyService,
        service: UserProfileService,
        *,
        rows: list[tuple[str, str]] | None = None,
    ) -> None:
        """Bind four optional row inputs and prefill each persisted position."""
        super().__init__()
        self.database = database
        self.settings = settings
        self.policy_service = policy_service
        self.service = service
        persisted = rows or []
        self.row_inputs: list[discord.ui.TextInput] = []
        for index in range(4):
            default = ""
            if index < len(persisted):
                name, value = persisted[index]
                default = f"{name}\n{value}"
            input_item = discord.ui.TextInput(
                label=f"Metadata field {index + 1}",
                required=False,
                max_length=511,
                style=discord.TextStyle.paragraph,
                placeholder="Name on first line\nValue on following lines",
                default=default,
            )
            self.row_inputs.append(input_item)
            self.add_item(input_item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Parse all four positions before one atomic metadata replacement."""
        user = await _resolve_authorized_user(
            interaction,
            database=self.database,
            settings=self.settings,
            policy_service=self.policy_service,
        )
        if user is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            parsed = [_parse_metadata_row(str(item)) for item in self.row_inputs]
            fields = [field for field in parsed if field is not None]
            result = await _run_owned_sync_mutation(
                lambda: self.service.replace_metadata(user.id, fields)
            )
        except UserProfileValidationError as exc:
            await interaction.edit_original_response(content=str(exc))
            return
        await _finish_profile_mutation(
            interaction,
            self.service,
            result,
            success_message="Profile metadata updated.",
        )


class ProfileMediaModal(discord.ui.Modal):
    """Replace one profile-media slot through Discord's modal file upload."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        policy_service: BridgePolicyService,
        service: UserProfileService,
        slot: str,
    ) -> None:
        """Create one typed avatar or header upload modal."""
        if slot not in {"avatar", "header"}:
            raise ValueError("invalid profile media slot")
        super().__init__(title=f"Upload {slot}")
        self.database = database
        self.settings = settings
        self.policy_service = policy_service
        self.service = service
        self.slot = slot
        self.upload = discord.ui.FileUpload(required=True, min_values=1, max_values=1)
        self.add_item(
            discord.ui.Label(text=f"{slot.title()} image", component=self.upload)
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Acknowledge, download once, and offload the complete sync mutation."""
        user = await _resolve_authorized_user(
            interaction,
            database=self.database,
            settings=self.settings,
            policy_service=self.policy_service,
        )
        if user is None:
            return
        attachments = list(self.upload.values)
        if len(attachments) != 1:
            await interaction.response.send_message(
                "Upload exactly one image.", ephemeral=True
            )
            return
        attachment = attachments[0]
        if attachment.size > self.settings.profile_media_max_upload_bytes:
            await interaction.response.send_message(
                "Image exceeds the configured upload limit.", ephemeral=True
            )
            return

        # Discord requires an initial response quickly; defer before network or
        # CPU/filesystem work so a successful commit cannot outlive its token.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            payload = await attachment.read()
            result = await _run_owned_sync_mutation(
                lambda: self.service.replace_media(user.id, self.slot, BytesIO(payload))
            )
        except UserProfileValidationError as exc:
            await interaction.edit_original_response(content=str(exc))
            return
        except Exception:
            logger.exception(
                "Profile media replacement failed user=%s slot=%s",
                interaction.user.id,
                self.slot,
            )
            await interaction.edit_original_response(
                content="The image could not be processed."
            )
            return
        await _finish_profile_mutation(
            interaction,
            self.service,
            result,
            success_message=f"{self.slot.title()} updated.",
        )


def register(
    tree: app_commands.CommandTree,
    database: Database,
    settings: Settings,
    policy_service: BridgePolicyService,
    service: UserProfileService,
    personal_feed_service: PersonalFeedService | None = None,
) -> None:
    """Register the registered-user-only ``/account-settings`` command family."""
    group = app_commands.Group(
        name="account-settings", description="Manage your ActivityPub profile"
    )

    async def authorized_profile(interaction: discord.Interaction):
        """Resolve current access and profile for one command invocation."""
        user = await _resolve_authorized_user(
            interaction,
            database=database,
            settings=settings,
            policy_service=policy_service,
        )
        if user is None:
            return None, None
        return user, service.get_profile(user.id)

    @group.command(
        name="view", description="View your current ActivityPub profile settings"
    )
    async def view(interaction: discord.Interaction) -> None:
        user, profile = await authorized_profile(interaction)
        if user is None or profile is None:
            return
        fields = (
            "\n".join(f"• {name}: {value}" for name, value in profile.fields) or "None"
        )
        await interaction.response.send_message(
            f"Handle: @{user.activitypub_username} (immutable)\nDisplay name: {profile.display_name}\nBio: {profile.summary or 'None'}\nMetadata:\n{fields}\nAvatar: {'set' if profile.avatar else 'not set'}\nHeader: {'set' if profile.header else 'not set'}",
            ephemeral=True,
        )

    @group.command(name="profile", description="Edit display name and bio")
    async def profile(interaction: discord.Interaction) -> None:
        user, current = await authorized_profile(interaction)
        if user is None or current is None:
            return
        await interaction.response.send_modal(
            ProfileTextModal(
                database,
                settings,
                policy_service,
                service,
                display_name=current.stored_display_name or "",
                summary=current.summary or "",
            )
        )

    @group.command(name="metadata", description="Edit up to four profile metadata rows")
    async def metadata(interaction: discord.Interaction) -> None:
        user, current = await authorized_profile(interaction)
        if user is None or current is None:
            return
        await interaction.response.send_modal(
            ProfileMetadataModal(
                database, settings, policy_service, service, rows=list(current.fields)
            )
        )

    @group.command(name="avatar", description="Upload a new profile avatar")
    async def avatar(interaction: discord.Interaction) -> None:
        if (await authorized_profile(interaction))[0] is None:
            return
        await interaction.response.send_modal(
            ProfileMediaModal(database, settings, policy_service, service, "avatar")
        )

    @group.command(name="header", description="Upload a new profile header")
    async def header(interaction: discord.Interaction) -> None:
        if (await authorized_profile(interaction))[0] is None:
            return
        await interaction.response.send_modal(
            ProfileMediaModal(database, settings, policy_service, service, "header")
        )

    async def clear_slot(interaction: discord.Interaction, slot: str) -> None:
        """Clear one slot while preserving all other profile state."""
        user, _ = await authorized_profile(interaction)
        if user is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await _run_owned_sync_mutation(
            lambda: service.clear_media(user.id, slot)
        )
        await _finish_profile_mutation(
            interaction, service, result, success_message=f"{slot.title()} cleared."
        )

    @group.command(name="clear-avatar", description="Remove your profile avatar")
    async def clear_avatar(interaction: discord.Interaction) -> None:
        await clear_slot(interaction, "avatar")

    @group.command(name="clear-header", description="Remove your profile header")
    async def clear_header(interaction: discord.Interaction) -> None:
        await clear_slot(interaction, "header")

    @group.command(
        name="feed-view", description="View your personal-feed channel binding"
    )
    async def feed_view(interaction: discord.Interaction) -> None:
        """Show the caller's current binding using durable IDs when cache data is absent."""
        user = await _resolve_authorized_user(
            interaction,
            database=database,
            settings=settings,
            policy_service=policy_service,
        )
        if user is None or personal_feed_service is None:
            return
        binding = personal_feed_service.get_by_discord_user_id(str(interaction.user.id))
        content = (
            "No personal-feed channel is bound."
            if binding is None
            else f"Personal feed: <#{binding.discord_channel_id}> (guild `{binding.discord_guild_id}`)."
        )
        await interaction.response.send_message(content, ephemeral=True)

    @group.command(
        name="feed-bind", description="Bind or move your personal-feed channel"
    )
    @app_commands.describe(channel="An ordinary guild text channel")
    async def feed_bind(
        interaction: discord.Interaction,
        channel: app_commands.Transform[
            discord.TextChannel, GuildTextChannelTransformer
        ],
    ) -> None:
        """Validate Discord authority and atomically bind one ordinary text channel."""
        user = await _resolve_authorized_user(
            interaction,
            database=database,
            settings=settings,
            policy_service=policy_service,
        )
        if user is None or personal_feed_service is None:
            return
        guild = interaction.guild
        member = interaction.user
        if guild is None or not getattr(
            getattr(member, "guild_permissions", None), "manage_guild", False
        ):
            await interaction.response.send_message(
                "You must have Manage Server permission to bind a personal feed.",
                ephemeral=True,
            )
            return
        if not is_normal_guild_text_channel(channel) or channel.guild.id != guild.id:
            await interaction.response.send_message(
                "Choose an ordinary text channel in this server; announcement channels are not supported.",
                ephemeral=True,
            )
            return
        # Snapshot the current row before worker submission. The service uses
        # this compare-and-set token so a delayed first-bind cannot overwrite
        # another request that acquired SQLite's writer reservation first.
        expected = personal_feed_service.get_by_discord_user_id(
            str(interaction.user.id)
        )
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await _run_owned_sync_mutation(
            lambda: personal_feed_service.bind_or_move(
                str(interaction.user.id),
                guild.id,
                channel.id,
                expected_binding_id=None if expected is None else expected.id,
            )
        )
        messages = {
            "bound": "Personal-feed channel bound.",
            "moved": "Personal-feed channel moved.",
            "already_bound": "That channel is already your personal feed.",
            "guild_conflict": "This server already has a personal-feed binding.",
            "channel_conflict": "That channel is already owned by another bridge mode.",
            "binding_changed": "Your personal-feed binding changed while this request was running. Try again.",
        }
        await interaction.edit_original_response(
            content=messages.get(
                result.status, "The personal-feed binding could not be changed."
            )
        )

    @group.command(
        name="feed-unbind", description="Remove your current personal-feed binding"
    )
    async def feed_unbind(interaction: discord.Interaction) -> None:
        """Remove only the caller's current binding through the owned sync worker."""
        user = await _resolve_authorized_user(
            interaction,
            database=database,
            settings=settings,
            policy_service=policy_service,
        )
        if user is None or personal_feed_service is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await _run_owned_sync_mutation(
            lambda: personal_feed_service.unbind(str(interaction.user.id))
        )
        content = (
            "Personal-feed binding removed."
            if result.status == "unbound"
            else "No personal-feed channel is bound."
        )
        await interaction.edit_original_response(content=content)

    tree.add_command(group)
