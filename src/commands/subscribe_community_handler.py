"""Submit-flow handler for the /subscribe-community Discord command.

This module keeps the Discord command registration adapter small by owning the
runtime subscription flow: community resolution, allowlist enforcement, optional
forum-channel placement, operation dispatch, cleanup, snapshots, and final
responses. Autocomplete stays in ``src.commands.subscribe`` because it is a
Discord option-wiring concern rather than submit handling.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable
from urllib.parse import urlparse

import discord
from discordops import run_operation_definition_async

from ..community_discovery import (
    CommunityResolutionError,
    ResolvedCommunity,
    infer_reference_origin,
    is_bridge_origin,
    normalize_instance_domain,
)
from ..community_labels import community_relay_label
from ..config import Settings
from ..db import Database
from ..discord_directory import record_discord_placement_snapshot
from ..discord_forum_placement import (
    ForumPlacement,
    ForumPlacementError,
    cleanup_created_forum_channel,
    derive_channel_name_from_community,
    resolve_optional_forum_channel,
)
from ..fedify_gateway_client import FedifyGatewayClient
from ..federation_policy import is_instance_allowed
from ..operations import SubscribeInput, subscribe_operation
from ..operations.subscribe_local_community import (
    SubscribeLocalCommunityInput,
    subscribe_local_community_operation,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SubscribeCommunityRequest:
    """Normalized Discord submit input for one /subscribe-community invocation.

    The dataclass carries only Discord-parsed values. It deliberately does not
    contain resolved community or placement state so each flow stage has a clear
    input/output contract.
    """

    interaction: discord.Interaction
    community: str
    channel: discord.ForumChannel | None
    instance_domain: str | None


@dataclass(slots=True)
class SubscribeTarget:
    """Resolved subscription target and origin data needed after discovery.

    ``selected_origin`` is the moderator-supplied instance option after
    normalization. ``resolved_origin`` is inferred from the final actor URL and
    is the source of truth for the post-resolution allowlist check.
    """

    resolved: ResolvedCommunity
    selected_origin: str | None
    resolved_origin: str | None


class SubscribeCommunityCommandHandler:
    """Run the /subscribe-community submit flow after Discord parses options.

    The handler preserves the old operation order while keeping ``register()``
    limited to command metadata and autocomplete wiring. Dependencies that are
    patched in tests are supplied as late-bound getters so existing tests can
    patch ``src.commands.subscribe`` without importing this module directly.
    """

    def __init__(
        self,
        *,
        database: Database,
        fedify_gateway: FedifyGatewayClient,
        settings: Settings | None,
        lemmy_client_cls_getter: Callable[[], type[Any]],
        resolve_selected_community_getter: Callable[[], Callable[..., Any]],
        fetch_bridge_communities_getter: Callable[[], Callable[..., Any]],
    ) -> None:
        """Store command dependencies and the allowlist snapshot for this registration."""
        self.database = database
        self.fedify_gateway = fedify_gateway
        self.settings = settings
        self.allowlist = settings.federation_allowlist if settings is not None else []
        self._lemmy_client_cls_getter = lemmy_client_cls_getter
        self._resolve_selected_community_getter = resolve_selected_community_getter
        self._fetch_bridge_communities_getter = fetch_bridge_communities_getter

    async def handle(
        self,
        *,
        interaction: discord.Interaction,
        community: str,
        channel: discord.ForumChannel | None,
        instance_domain: str | None,
    ) -> None:
        """Run the complete submit flow and send the Discord response.

        The method is intentionally linear: resolve community identity, place
        the Discord forum channel, run the domain operation, then cleanup or
        snapshot based on the operation result.
        """
        request = SubscribeCommunityRequest(
            interaction=interaction,
            community=community,
            channel=channel,
            instance_domain=instance_domain,
        )
        target = await self._resolve_target(request)
        if target is None:
            return
        target = await self._ensure_numeric_id(request, target)
        if target is None:
            return
        placement = await self._resolve_forum_placement(request, target)
        if placement is None:
            return
        await self._run_with_placement_cleanup(request, target, placement)

    async def _resolve_target(
        self,
        request: SubscribeCommunityRequest,
    ) -> SubscribeTarget | None:
        """Resolve the community and enforce allowlist checks before placement."""
        origins = await self._infer_candidate_origin(request)
        if origins is None:
            return None
        selected_origin, candidate_origin = origins
        if not await self._ensure_origin_allowed(request, candidate_origin):
            return None

        try:
            resolved = await self._resolve_selected(request)
        except CommunityResolutionError as error:
            logger.warning("Failed to resolve subscribe target %s: %s", request.community, error)
            await request.interaction.response.send_message(str(error), ephemeral=True)
            return None

        resolved_origin = infer_reference_origin(resolved.actor_id) or selected_origin
        # The raw command value is user-controlled even when it came from an
        # autocomplete choice, so the resolved actor URL is checked again before
        # any Discord channel creation or persistence can occur.
        if not await self._ensure_origin_allowed(request, resolved_origin):
            return None
        return SubscribeTarget(
            resolved=resolved,
            selected_origin=selected_origin,
            resolved_origin=resolved_origin,
        )

    async def _infer_candidate_origin(
        self,
        request: SubscribeCommunityRequest,
    ) -> tuple[str | None, str | None] | None:
        """Infer selected and candidate origins from raw command options."""
        try:
            inferred_origin = infer_reference_origin(request.community)
        except CommunityResolutionError as error:
            await request.interaction.response.send_message(str(error), ephemeral=True)
            return None

        raw_instance = (request.instance_domain or "").strip()
        try:
            selected_origin = normalize_instance_domain(raw_instance) if raw_instance else None
        except CommunityResolutionError as error:
            await request.interaction.response.send_message(str(error), ephemeral=True)
            return None

        # Encoded direct-mode payloads stay scoped to the selected instance.
        # Plain Lemmyverse actor URLs infer their candidate origin from the URL.
        candidate_origin = selected_origin if ("|" in request.community and selected_origin is not None) else inferred_origin
        return selected_origin, candidate_origin

    async def _ensure_origin_allowed(
        self,
        request: SubscribeCommunityRequest,
        origin: str | None,
    ) -> bool:
        """Reject disallowed non-bridge origins with the current message text."""
        if origin is None or is_bridge_origin(origin, self.settings) or is_instance_allowed(origin, self.allowlist):
            return True
        hostname = urlparse(origin).hostname or origin
        await request.interaction.response.send_message(
            f"Instance **{hostname}** is not in the federation allowlist.",
            ephemeral=True,
        )
        return False

    async def _resolve_selected(self, request: SubscribeCommunityRequest) -> ResolvedCommunity:
        """Resolve the selected community through the existing discovery path."""
        resolver = self._resolve_selected_community_getter()
        return await resolver(
            self.settings,
            instance_domain=request.instance_domain,
            community_value=request.community,
            fetch_bridge_communities=self._fetch_bridge_communities_getter(),
            lemmy_client_cls=self._lemmy_client_cls_getter(),
        )

    async def _ensure_numeric_id(
        self,
        request: SubscribeCommunityRequest,
        target: SubscribeTarget,
    ) -> SubscribeTarget | None:
        """Backfill Lemmy numeric community id for remote Lemmy targets when needed."""
        resolved = target.resolved
        if resolved.source != "remote_lemmy" or resolved.numeric_id is not None:
            return target
        if target.resolved_origin is None:
            await request.interaction.response.send_message(
                "Could not infer the Lemmy community origin. Please provide instance_domain.",
                ephemeral=True,
            )
            return None

        client = self._lemmy_client_cls_getter()(target.resolved_origin)
        try:
            numeric_id = await client.resolve_community_id(name=resolved.name or resolved.actor_id)
        except Exception:
            logger.exception("Failed to resolve community ID for %s", resolved.actor_id)
            await request.interaction.response.send_message(
                "Could not resolve the Lemmy community ID. Please try again.",
                ephemeral=True,
            )
            return None
        finally:
            await client.close()

        # Rebuild the immutable resolution record with only the numeric id
        # filled in. This preserves the discovery contract for operation inputs.
        return SubscribeTarget(
            resolved=ResolvedCommunity(
                source=resolved.source,
                actor_id=resolved.actor_id,
                name=resolved.name,
                numeric_id=numeric_id,
                handle=resolved.handle,
                local_community_id=resolved.local_community_id,
                remote_software=resolved.remote_software,
            ),
            selected_origin=target.selected_origin,
            resolved_origin=target.resolved_origin,
        )

    async def _resolve_forum_placement(
        self,
        request: SubscribeCommunityRequest,
        target: SubscribeTarget,
    ) -> ForumPlacement | None:
        """Choose or create the final Discord forum channel for the target."""
        resolved = target.resolved
        desired_name = derive_channel_name_from_community(
            name=resolved.name,
            handle=resolved.handle,
            actor_id=resolved.actor_id,
        )
        try:
            return await resolve_optional_forum_channel(
                database=self.database,
                guild=request.interaction.guild,
                selected_channel=request.channel,
                desired_name=desired_name,
                command_name="subscribe-community",
                remote_subscription_blocking_statuses={"pending", "accepted"},
            )
        except ForumPlacementError as error:
            await request.interaction.response.send_message(error.message, ephemeral=True)
            return None

    async def _run_with_placement_cleanup(
        self,
        request: SubscribeCommunityRequest,
        target: SubscribeTarget,
        placement: ForumPlacement,
    ) -> None:
        """Run the operation and cleanup bot-created channels on later failures."""
        try:
            result = await self._run_subscription_operation(request, target, placement)
        except Exception:
            # The forum channel may have been created by this command before the
            # domain operation raised. Cleanup remains best-effort and guarded by
            # DB ownership checks in the placement helper.
            await cleanup_created_forum_channel(
                placement,
                database=self.database,
                logger=logger,
                guild_id=request.interaction.guild_id,
                command_name="subscribe-community",
                original_reason="unexpected_exception",
            )
            raise

        if getattr(result, "reason", None) == "follow_dispatch_failed":
            logger.error("Failed to send follow for community %s", target.resolved.actor_id)
        if not result.applied:
            await cleanup_created_forum_channel(
                placement,
                database=self.database,
                logger=logger,
                guild_id=request.interaction.guild_id,
                command_name="subscribe-community",
                original_reason=result.reason,
            )
        else:
            # Snapshots describe committed routing state, so they are recorded
            # only after the domain operation succeeds.
            record_discord_placement_snapshot(
                self.database,
                guild=request.interaction.guild,
                channel=placement.channel,
            )
            logger.info("Subscribed channel %s to community %s", placement.channel.id, target.resolved.actor_id)

        await self._send_operation_response(request, target, placement, result)

    async def _run_subscription_operation(
        self,
        request: SubscribeCommunityRequest,
        target: SubscribeTarget,
        placement: ForumPlacement,
    ) -> Any:
        """Dispatch to local-bridge or remote-community subscription operation."""
        if target.resolved.source == "local_bridge":
            return await self._run_local_subscribe(request, target, placement)
        return await self._run_remote_subscribe(request, target, placement)

    async def _run_local_subscribe(
        self,
        request: SubscribeCommunityRequest,
        target: SubscribeTarget,
        placement: ForumPlacement,
    ) -> Any:
        """Subscribe a Discord forum channel to a bridge-owned local community."""
        resolved = target.resolved
        return await run_operation_definition_async(
            subscribe_local_community_operation,
            SubscribeLocalCommunityInput(
                database=self.database,
                discord_user_id=str(request.interaction.user.id),
                guild_id=request.interaction.guild_id,
                channel_id=placement.channel.id,
                channel_mention=placement.channel.mention,
                local_community_id=int(resolved.local_community_id),
                local_community_name=resolved.name or resolved.handle,
            ),
        )

    async def _run_remote_subscribe(
        self,
        request: SubscribeCommunityRequest,
        target: SubscribeTarget,
        placement: ForumPlacement,
    ) -> Any:
        """Subscribe a Discord forum channel to a remote community actor."""
        resolved = target.resolved
        return await run_operation_definition_async(
            subscribe_operation,
            SubscribeInput(
                database=self.database,
                fedify_gateway=self.fedify_gateway,
                discord_user_id=str(request.interaction.user.id),
                guild_id=request.interaction.guild_id,
                channel_id=placement.channel.id,
                channel_mention=placement.channel.mention,
                actor_id=resolved.actor_id,
                community_name=resolved.name,
                numeric_id=resolved.numeric_id,
                community_handle=resolved.handle,
            ),
        )

    async def _send_operation_response(
        self,
        request: SubscribeCommunityRequest,
        target: SubscribeTarget,
        placement: ForumPlacement,
        result: Any,
    ) -> None:
        """Send the final Discord response while preserving public formatting."""
        message = result.message
        send_kwargs: dict[str, object] = {"ephemeral": not result.applied}
        if result.applied:
            message = _subscribe_success_message(
                user_id=request.interaction.user.id,
                channel_mention=placement.channel.mention,
                actor_id=target.resolved.actor_id,
                name=target.resolved.name,
                handle=target.resolved.handle,
                waiting_for_accept="Waiting for federation acceptance." in result.message,
            )
            send_kwargs["allowed_mentions"] = _no_ping_allowed_mentions()
        await request.interaction.response.send_message(message, **send_kwargs)


def _no_ping_allowed_mentions() -> discord.AllowedMentions:
    """Suppress notification delivery for display-only user mentions."""
    # The content still uses normal mention markup so Discord renders a
    # clickable user reference, but allowed_mentions disables notification
    # parsing for that mention.
    return discord.AllowedMentions.none()


def _user_mention(user_id: object) -> str:
    """Return Discord mention markup for a command initiator."""
    return f"<@{user_id}>"


def _subscribe_success_message(
    *,
    user_id: object,
    channel_mention: str,
    actor_id: str | None,
    name: str | None,
    handle: str | None,
    waiting_for_accept: bool,
) -> str:
    """Build the public success message for /subscribe-community."""
    label = community_relay_label(actor_id=actor_id, name=name, handle=handle)
    message = f"{_user_mention(user_id)} subscribed {channel_mention} to **{label}**."
    if waiting_for_accept:
        return f"{message} Waiting for federation acceptance."
    return message
