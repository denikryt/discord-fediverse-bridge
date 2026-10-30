"""SDK-boundary fake that forwards raw notices as verified typed mutation DTOs.

This is test-only: DiscordAdapter's own raw normalization and SDK admission are
covered separately. These scenarios retain the real BridgeBot callback,
DiscordMessageMutationHandler and SQLite routing path without live Discord I/O.
"""

from __future__ import annotations

from datetime import datetime

from src.platform_types import (
    ConnectorRef, ContentDocument, MessageDeleted, MessageRef, MessageUpdated,
    PlatformOperationError,
)


class TypedMutationIngress:
    """Exercise the bot -> neutral mutation handler path with no SDK reads."""

    connector = ConnectorRef("discord-primary", "discord")

    def __init__(self, handler: object) -> None:
        """Route normalized events into the real durability/mutation handler."""
        self.handler = handler

    def enter_ingress(self) -> bool:
        """Admit a synthetic Gateway callback."""
        return True

    def leave_ingress(self) -> None:
        """Balance admission when the callback finishes."""

    async def ingest_raw_message_edit(self, payload: object) -> None:
        """Project the untrusted raw edit to the narrow platform mutation DTO."""
        data = payload.data
        timestamp = data.get("edited_timestamp")
        edited_at = (
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if isinstance(timestamp, str) else None
        )
        # An absent body is unknown, not an empty edit. The handler will
        # attempt one adapter read and persist reconciliation if unavailable.
        body = data.get("content")
        event = MessageUpdated(
            connector_key=self.connector.key,
            message=MessageRef(
                self.connector.key, str(payload.channel_id), str(payload.message_id)
            ),
            content=ContentDocument(body) if isinstance(body, str) else None,
            edited_at=edited_at,
            external_guild_id=(
                str(payload.guild_id) if payload.guild_id is not None else None
            ),
        )
        await self.handler.handle_platform_message_edit(event, self)

    async def ingest_raw_message_delete(self, payload: object) -> None:
        """Preserve the exact message ID without requiring a timestamp."""
        event = MessageDeleted(
            connector_key=self.connector.key,
            message=MessageRef(
                self.connector.key, str(payload.channel_id), str(payload.message_id)
            ),
            external_guild_id=(
                str(payload.guild_id) if payload.guild_id is not None else None
            ),
        )
        await self.handler.handle_platform_message_delete(event, self)

    async def fetch_message(self, ref: MessageRef) -> object:
        """Refuse network access so a partial edit exercises durable recovery."""
        raise PlatformOperationError("unavailable", "isolated Discord fetch failed")
