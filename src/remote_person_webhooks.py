"""Discord Incoming Webhook adapter for remote Person root publications only.

Credentials are fetched from Discord and never persisted or logged. The
canonical remote Actor URL supplies the profile namespace: webhook names and
avatars are presentation data, never an authorization identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

from .remote_person_feeds import REQUIRED_BOT_PERMISSIONS
from .formatting import render_feed_discord_message, validate_feed_object_url
from .federated_media import (can_attach_files, discord_files, download_images,
    image_presentation, note_from_activity)


class RemoteWebhookUnavailable(RuntimeError):
    """The saved root's original incoming webhook cannot be authenticated."""


@dataclass(frozen=True)
class PreparedRemoteWebhook:
    """In-memory authenticated Incoming webhook and its owning text channel."""

    channel: object
    webhook: object

    @property
    def webhook_id(self) -> int:
        """Expose the public webhook identity without retaining its token."""
        return int(self.webhook.id)


def _safe_image_url(value: str | None) -> str | None:
    """Accept HTTPS public-host avatar URLs; Discord fetches the image itself."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        return None
    if parsed.fragment or parsed.hostname.lower() == 'localhost':
        return None
    try:
        ip = ip_address(parsed.hostname)
        if not ip.is_global:
            return None
    except ValueError:
        if '.' not in parsed.hostname:
            return None
    return value


def _person_name(post: object) -> str:
    """Keep Actor.name intact while removing only a known duplicate host suffix."""
    actor = urlsplit(post.remote_actor_id)
    fallback = actor.path.rstrip('/').rsplit('/', 1)[-1]
    name = str(post.remote_handle or fallback).strip().strip('@')
    suffix = f'@{actor.hostname}'
    # `Alice @ Work` is a display name, not a handle to split at the first @.
    if name.lower().endswith(suffix.lower()):
        name = name[:-len(suffix)]
    return name.strip() or fallback


def _handle(post: object) -> str:
    """Render the incoming webhook display from the actor's canonical domain."""
    actor = urlsplit(post.remote_actor_id)
    return f'{_person_name(post)}@{actor.hostname}'[:80]


def remote_root_presentation(post: object) -> tuple[str, tuple[str, ...]]:
    """Use the saved Note to render its links and select its current images."""
    note = note_from_activity(post.source_activity_json)
    warning = str(note.get('summary') or '').strip()
    lines: list[str] = []
    if warning:
        lines.append(f'Content warning: {warning}')
    body, urls = image_presentation(str(post.content_markdown or '').strip(), note,
        str(post.object_id), include_other_attachments=True)
    if body:
        lines.append(body)
    footer = validate_feed_object_url(str(post.object_id))
    return render_feed_discord_message(header='', body='\n\n'.join(lines), footer=footer), urls


def remote_root_content(post: object) -> str:
    """Render a webhook root without duplicating image links or author identity."""
    return remote_root_presentation(post)[0]


class RemotePersonWebhookTransport:
    """Translate root create/edit/delete/thread effects into discord.py calls."""

    def __init__(self, bot: object) -> None:
        """Use only the existing bot token and its own type-1 webhooks."""
        self.bot = bot

    async def _channel(self, channel_id: int) -> object:
        """Require a guild text channel with current effective bot permissions."""
        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        channel_type = getattr(getattr(channel, 'type', None), 'value', getattr(channel, 'type', None))
        guild = getattr(channel, 'guild', None)
        if channel_type != 0 or guild is None or getattr(guild, 'me', None) is None:
            raise PermissionError('destination is not an accessible guild text channel')
        flags = channel.permissions_for(guild.me)
        missing = [name for name in REQUIRED_BOT_PERMISSIONS if not getattr(flags, name, False)]
        if missing:
            raise PermissionError(f'bot channel permissions revoked: {", ".join(missing)}')
        return channel

    @staticmethod
    def _valid_webhook(webhook: object, channel_id: int, bot_id: int) -> bool:
        """Use only Incoming type 1 credentials created by this bot in the channel."""
        kind = getattr(getattr(webhook, 'type', None), 'value', getattr(webhook, 'type', None))
        owner = getattr(getattr(webhook, 'user', None), 'id', None)
        return bool(kind == 1 and int(getattr(webhook, 'channel_id', 0) or 0) == channel_id
                    and owner == bot_id and getattr(webhook, 'token', None))

    async def prepare(self, channel_id: int, *, original_webhook_id: int | None = None) -> PreparedRemoteWebhook:
        """Resolve a usable Incoming webhook, creating one only for new roots."""
        channel = await self._channel(channel_id)
        bot_id = getattr(getattr(self.bot, 'user', None), 'id', None)
        if bot_id is None:
            raise RuntimeError('Discord bot user identity is unavailable')
        webhooks = await channel.webhooks()
        eligible = [w for w in webhooks if self._valid_webhook(w, channel_id, bot_id)]
        if original_webhook_id is not None:
            webhook = next((w for w in eligible if int(w.id) == original_webhook_id), None)
            if webhook is None:
                raise RemoteWebhookUnavailable('original webhook or its credentials are unavailable')
            return PreparedRemoteWebhook(channel, webhook)
        webhook = next((w for w in eligible if getattr(w, 'name', '') == 'Fediverse Bridge'), None)
        if webhook is None:
            webhook = await channel.create_webhook(name='Fediverse Bridge')
            if not self._valid_webhook(webhook, channel_id, bot_id):
                raise RemoteWebhookUnavailable('new webhook is not bot-owned Incoming type 1')
        return PreparedRemoteWebhook(channel, webhook)

    async def create(self, prepared: PreparedRemoteWebhook, post: object) -> int:
        """Post a root under the remote author's handle and avatar with wait=true."""
        import discord
        # The worker downloads files while still claimed, before authorizing
        # the webhook POST. Never introduce another media GET after that fence.
        images = getattr(post, 'images', ()) if can_attach_files(prepared.channel) else ()
        with discord_files(images) as files:
            options = {'files': files} if files else {}
            message = await prepared.webhook.send(
                content=remote_root_content(post),
                username=_handle(post), avatar_url=_safe_image_url(post.remote_avatar_url),
                allowed_mentions=discord.AllowedMentions.none(), wait=True,
                suppress_embeds=True, **options,
            )
        if message is None or getattr(message, 'id', None) is None:
            raise RuntimeError('wait=true webhook POST returned no Discord message ID')
        return int(message.id)

    async def create_thread(self, channel_id: int, root_message_id: int, post: object) -> int:
        """Create a public text-channel thread as the bot, not as the webhook."""
        channel = await self._channel(channel_id)
        message = await channel.fetch_message(root_message_id)
        # Media-only Notes have no first line; the fallback is a real thread title.
        title = next((line.strip() for line in str(post.content_markdown or '').splitlines()
                      if line.strip()), '')[:90] or 'Fediverse discussion'
        thread = await message.create_thread(name=title)
        if int(thread.id) != root_message_id:
            raise RuntimeError('Discord root thread identity differs from starter message ID')
        return int(thread.id)

    async def find_created_root(self, channel_id: int, original_webhook_id: int, object_id: str) -> int | None:
        """Reconcile ambiguous POST by matching this webhook and canonical URL."""
        channel = await self._channel(channel_id)
        matched: int | None = None
        async for message in channel.history(limit=100):
            if (getattr(message, 'webhook_id', None) == original_webhook_id
                    and str(getattr(message, 'content', '')).splitlines()[-1:] == [object_id]):
                # Duplicate history matches are not proof of which original
                # POST owns this durable placement. Keep it uncertain.
                if matched is not None:
                    return None
                matched = int(message.id)
        return matched

    async def edit(self, prepared: PreparedRemoteWebhook, message_id: int, post: object) -> None:
        """Mutate only a known message using its original webhook credentials."""
        import discord
        content, urls = remote_root_presentation(post)
        images = await download_images(urls) if can_attach_files(prepared.channel) else ()
        # discord.py's webhook PATCH omits flags, preserving SUPPRESS_EMBEDS.
        # An explicit replacement list removes old files on image-less edits.
        with discord_files(images) as files:
            await prepared.webhook.edit_message(message_id, content=content,
                attachments=files, allowed_mentions=discord.AllowedMentions.none())

    async def delete(self, prepared: PreparedRemoteWebhook, message_id: int) -> None:
        """Delete the retained root using the same original Incoming webhook."""
        await prepared.webhook.delete_message(message_id)
