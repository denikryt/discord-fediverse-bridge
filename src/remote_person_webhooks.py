"""Discord Incoming Webhook adapter for remote Person root publications only.

Credentials are fetched from Discord and never persisted or logged. The
canonical remote Actor URL supplies the profile namespace: webhook names and
avatars are presentation data, never an authorization identity.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

from .remote_person_feeds import REQUIRED_BOT_PERMISSIONS
from .formatting import (feed_author_header, render_feed_discord_message,
                         validate_feed_object_url)


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


def _handle(post: object) -> str:
    """Render an unambiguous handle using the canonical remote actor's host."""
    actor = urlsplit(post.remote_actor_id)
    raw = str(post.remote_handle or actor.path.rsplit('/', 1)[-1]).strip('@')
    # A remote display name is never allowed to replace the canonical host.
    name = raw.split('@', 1)[0].strip() or actor.path.rsplit('/', 1)[-1]
    return f'{name}@{actor.hostname}'[:80]


def _attachment_urls(source: object) -> list[str]:
    """Extract only safe external media links from the signed public Note."""
    if not isinstance(source, dict):
        return []
    note = source.get('object')
    if not isinstance(note, dict):
        return []
    attachments = note.get('attachment')
    values = attachments if isinstance(attachments, list) else [attachments]
    urls: list[str] = []
    for value in values:
        url = value.get('url') if isinstance(value, dict) else None
        if isinstance(url, dict):
            url = url.get('href') or url.get('id')
        if isinstance(url, str) and _safe_image_url(url) and url not in urls:
            urls.append(url)
    return urls[:5]


def remote_root_content(post: object) -> str:
    """Render bridge-owned webhook root with fixed author and source identity."""
    try:
        source = json.loads(post.source_activity_json)
    except (TypeError, ValueError):
        source = {}
    note = source.get('object') if isinstance(source, dict) else None
    note = note if isinstance(note, dict) else {}
    warning = str(note.get('summary') or '').strip()
    lines: list[str] = []
    if warning:
        lines.append(f'Content warning: {warning}')
    body = str(post.content_markdown or '').strip()
    if body:
        lines.append(body)
    lines.extend(_attachment_urls(source))
    actor = urlsplit(post.remote_actor_id)
    name = str(post.remote_handle or actor.path.rsplit('/', 1)[-1]).strip('@')
    # A saved handle may already include the instance host; the Community
    # formatter adds the host exactly once from the canonical Actor ID.
    name = name.split('@', 1)[0]
    header = feed_author_header(name, post.remote_actor_id)
    footer = validate_feed_object_url(str(post.object_id))
    return render_feed_discord_message(
        header=header, body='\n\n'.join(lines), footer=footer,
    )


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
        message = await prepared.webhook.send(
            content=remote_root_content(post),
            username=_handle(post), avatar_url=_safe_image_url(post.remote_avatar_url),
            allowed_mentions=discord.AllowedMentions.none(), wait=True,
            suppress_embeds=True,
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
        await prepared.webhook.edit_message(message_id, content=remote_root_content(post),
                                             allowed_mentions=discord.AllowedMentions.none())

    async def delete(self, prepared: PreparedRemoteWebhook, message_id: int) -> None:
        """Delete the retained root using the same original Incoming webhook."""
        await prepared.webhook.delete_message(message_id)
