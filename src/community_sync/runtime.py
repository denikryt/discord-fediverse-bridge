"""Central orchestration entry point for shared community sync.

CommunityRuntime is the single call boundary for all thread/message events in
both directions (Discord→AP and AP→Discord). Phase 0 is a thin pass-through
stub; Phase 2+ will own thread/message group creation and Discord fanout.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord

    from ..activitypub_handlers import HandlerResult
    from ..activitypub_models import ActivityPubEvent
    from ..db import Database
    from ..discord_publish_service import DiscordPublishService, PublishResult
    from ..runtime import Runtime

logger = logging.getLogger(__name__)


class CommunityRuntime:
    """Orchestrate all shared community sync events through one stable call boundary.

    Phase 0: delegates every call directly to the existing DiscordPublishService
    and activitypub_handlers helpers. No new logic is introduced here.
    Phase 2+: will create CommunityThreadGroup/CommunityMessageGroup rows,
    drive AP publish, and fan out to all subscribed Discord channels.
    """

    def __init__(
        self,
        *,
        database: Database,
        discord_publish_service: DiscordPublishService,
    ) -> None:
        """Initialise the runtime with the shared database and publish service."""
        self.database = database
        self.discord_publish_service = discord_publish_service

    async def handle_discord_thread_create(
        self,
        thread: discord.Thread,
        starter_message: discord.Message,
    ) -> PublishResult:
        """Handle a new Discord forum thread from a subscribed channel.

        Phase 0: delegates directly to DiscordPublishService.publish_thread_starter.
        Phase 2+: will create a CommunityThreadGroup, call the AP gateway,
        and fan out the thread to sibling subscribed channels.
        """
        # Pass through to existing publish logic unchanged.
        return await self.discord_publish_service.publish_thread_starter(
            thread=thread,
            starter_message=starter_message,
        )

    async def handle_discord_message(
        self,
        message: discord.Message,
    ) -> PublishResult:
        """Handle a new Discord thread message from a subscribed channel.

        Phase 0: delegates directly to DiscordPublishService.publish_thread_message.
        Phase 2+: will create a CommunityMessageGroup, call the AP gateway,
        and fan out the message to sibling subscribed channels.
        """
        # Pass through to existing publish logic unchanged.
        return await self.discord_publish_service.publish_thread_message(
            message=message,
        )

    async def handle_inbound_post(
        self,
        event: ActivityPubEvent,
        runtime: Runtime,
    ) -> HandlerResult:
        """Handle an inbound ActivityPub post event.

        Phase 0: delegates to the private _deliver_post helper in
        activitypub_handlers, bypassing the public handle_post_created entry
        point to avoid a circular call chain.
        Phase 2+: will own echo suppression, CommunityThreadGroup creation,
        and multi-channel fanout directly.
        """
        # Lazy import avoids a circular module dependency: activitypub_handlers
        # imports Runtime which imports CommunityRuntime. The import is safe at
        # call time because all modules are fully initialised by then.
        from ..activitypub_handlers import _deliver_post
        return await _deliver_post(event, runtime)

    async def handle_inbound_comment(
        self,
        event: ActivityPubEvent,
        runtime: Runtime,
    ) -> HandlerResult:
        """Handle an inbound ActivityPub comment event.

        Phase 0: delegates to the private _deliver_comment helper in
        activitypub_handlers, bypassing the public handle_comment_created entry
        point to avoid a circular call chain.
        Phase 2+: will own echo suppression, CommunityMessageGroup creation,
        and multi-channel fanout directly.
        """
        # Lazy import for the same circular-dependency reason as handle_inbound_post.
        from ..activitypub_handlers import _deliver_comment
        return await _deliver_comment(event, runtime)
