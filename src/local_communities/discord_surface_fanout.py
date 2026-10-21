"""Runtime-level local Discord fanout selection for local communities.

This wrapper owns the runtime decisions around whether a Discord client is bound
and which concrete LocalCommunityDiscordFanout operation to call.  It does not
own Discord SDK operations; those remain inside `discord_fanout.py`.
"""

from __future__ import annotations

from collections.abc import Callable

from ..bridge_policy import BridgePolicyService
from ..db import Database
from ..runtime_ports import DiscordDeliveryClientPort, PlatformAdapterPort
from .discord_fanout import LocalCommunityDiscordFanout


class LocalCommunitySurfaceFanout:
    """Bridge runtime fanout decisions to concrete local Discord fanout operations."""

    def __init__(
        self,
        *,
        database: Database,
        policy_service: BridgePolicyService,
        discord_client_provider: Callable[[], DiscordDeliveryClientPort | None],
    ) -> None:
        """Store narrow dependencies needed to attempt best-effort local fanout."""
        self.database = database
        self.policy_service = policy_service
        self._discord_client_provider = discord_client_provider

    def _fanout(self, adapter: PlatformAdapterPort | None = None) -> LocalCommunityDiscordFanout | None:
        """Create the fanout helper with a usable SDK or normalized platform port."""
        bot = self._discord_client_provider()
        if bot is None and adapter is None:
            return None
        return LocalCommunityDiscordFanout(
            database=self.database,
            bot=bot,
            policy_service=self.policy_service,
        )

    async def fanout_thread_to_local_subscribers(
        self,
        *,
        local_community: object,
        thread_row: object,
        title: str,
        content: str,
        author_display_name: str | None,
        source_forum_channel_id: int | None,
    ) -> None:
        """Best-effort Discord adapter integration fanout of one canonical post to subscribers."""
        await self.fanout_thread_to_local_participants(
            local_community=local_community,
            thread_row=thread_row,
            title=title,
            content=content,
            author_display_name=author_display_name,
            source_forum_channel_id=source_forum_channel_id,
            include_host=False,
        )

    async def fanout_thread_to_local_participants(
        self,
        *,
        local_community: object,
        thread_row: object,
        title: str,
        content: str,
        author_display_name: str | None,
        source_forum_channel_id: int | None,
        include_host: bool,
    ) -> None:
        """Best-effort fanout of one canonical post to selected local surfaces."""
        fanout = self._fanout()
        if fanout is None:
            return
        await fanout.fanout_thread(
            local_community=local_community,
            thread_row=thread_row,
            title=title,
            content=content,
            author_display_name=author_display_name,
            source_forum_channel_id=source_forum_channel_id,
            include_host=include_host,
        )

    async def fanout_message_to_local_subscribers(
        self,
        *,
        local_community: object,
        thread_row: object,
        message_row: object,
        content: str,
        author_display_name: str | None,
        source_forum_channel_id: int | None,
    ) -> None:
        """Best-effort Discord adapter integration fanout of one canonical comment to subscribers."""
        await self.fanout_message_to_local_participants(
            local_community=local_community,
            thread_row=thread_row,
            message_row=message_row,
            content=content,
            author_display_name=author_display_name,
            source_forum_channel_id=source_forum_channel_id,
            include_host=False,
        )

    async def fanout_message_to_local_participants(
        self,
        *,
        local_community: object,
        thread_row: object,
        message_row: object,
        content: str,
        author_display_name: str | None,
        source_forum_channel_id: int | None,
        include_host: bool,
    ) -> None:
        """Best-effort fanout of one canonical comment to selected local surfaces."""
        fanout = self._fanout()
        if fanout is None:
            return
        await fanout.fanout_message(
            local_community=local_community,
            thread_row=thread_row,
            message_row=message_row,
            content=content,
            author_display_name=author_display_name,
            source_forum_channel_id=source_forum_channel_id,
            include_host=include_host,
        )

    async def fanout_thread_edit(
        self,
        *,
        thread_row: object,
        source_surface_id: int | None,
        new_content: str,
        adapter: PlatformAdapterPort | None = None,
    ) -> None:
        """Best-effort edit fanout for starter surfaces of one canonical post."""
        fanout = self._fanout(adapter)
        if fanout is None:
            return
        await fanout.fanout_thread_starter_edit(
            thread_row=thread_row,
            source_surface_id=source_surface_id,
            new_content=new_content,
            adapter=adapter,
        )

    async def fanout_message_edit(
        self,
        *,
        message_row: object,
        source_surface_id: int | None,
        new_content: str,
        adapter: PlatformAdapterPort | None = None,
    ) -> None:
        """Best-effort edit fanout for message surfaces of one canonical comment."""
        fanout = self._fanout(adapter)
        if fanout is None:
            return
        await fanout.fanout_message_edit(
            message_row=message_row,
            source_surface_id=source_surface_id,
            new_content=new_content,
            adapter=adapter,
        )

    async def fanout_thread_delete(
        self, *, thread_row: object, source_surface_id: int | None,
        adapter: PlatformAdapterPort | None = None,
    ) -> None:
        """Best-effort delete marker fanout for starter surfaces."""
        fanout = self._fanout(adapter)
        if fanout is None:
            return
        await fanout.fanout_thread_starter_delete(
            thread_row=thread_row, source_surface_id=source_surface_id,
            adapter=adapter,
        )

    async def fanout_message_delete(
        self, *, message_row: object, source_surface_id: int | None,
        adapter: PlatformAdapterPort | None = None,
    ) -> None:
        """Best-effort delete marker fanout for message surfaces."""
        fanout = self._fanout(adapter)
        if fanout is None:
            return
        await fanout.fanout_message_delete(
            message_row=message_row, source_surface_id=source_surface_id,
            adapter=adapter,
        )
