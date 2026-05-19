"""Discord event routing boundary between bridge modes.

This dispatcher keeps `BridgeBot` as a Discord adapter. It decides whether a
forum event belongs to the existing remote-subscription mode, the new
local-community mode, or neither, then forwards the event to the right runtime.
"""

from __future__ import annotations

from .db import Database
from .community_sync.runtime import CommunityRuntime
from .local_communities.runtime import LocalCommunityRuntime


class DiscordEventRouter:
    """Route Discord thread/message events to the correct bridge runtime."""

    def __init__(
        self,
        *,
        database: Database,
        community_runtime: CommunityRuntime,
        local_community_runtime: LocalCommunityRuntime,
    ) -> None:
        """Initialise the router with the runtimes for both bridge modes."""
        self.database = database
        self.community_runtime = community_runtime
        self.local_community_runtime = local_community_runtime

    def is_local_community_forum(self, forum_channel_id: int | None) -> bool:
        """Return whether the given forum channel is a local federated community."""
        if forum_channel_id is None:
            return False
        return self.database.get_local_community_by_forum_channel_id(forum_channel_id) is not None

    def is_remote_subscription_forum(self, forum_channel_id: int | None) -> bool:
        """Return whether the given forum channel is subscribed to a remote community."""
        if forum_channel_id is None:
            return False
        return self.database.get_subscription_by_channel(forum_channel_id) is not None

    async def handle_thread_create(self, *, thread: object, starter_message: object) -> object:
        """Route one thread-create event into the owning bridge mode."""
        forum_channel_id = getattr(thread, "parent_id", None)
        if self.is_local_community_forum(forum_channel_id):
            return await self.local_community_runtime.handle_discord_thread_create(
                thread=thread,
                starter_message=starter_message,
            )
        return await self.community_runtime.handle_discord_thread_create(
            thread=thread,
            starter_message=starter_message,
        )

    async def handle_message(self, *, message: object) -> object:
        """Route one message event into the owning bridge mode."""
        thread = getattr(message, "channel")
        forum_channel_id = getattr(thread, "parent_id", None)
        if self.is_local_community_forum(forum_channel_id):
            return await self.local_community_runtime.handle_discord_message(
                message=message,
            )
        return await self.community_runtime.handle_discord_message(
            message=message,
        )
