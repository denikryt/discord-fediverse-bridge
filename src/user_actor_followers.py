"""Inbound Follow and Undo lifecycle for registered local User Actors."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from .inbound_activity_outcomes import InboundActivityOutcome
from .remote_delivery_endpoints import (
    RemoteDeliveryEndpointError,
    admit_remote_delivery_endpoint,
)
from .runtime_ports import DiscordUserLookupPort

logger = logging.getLogger(__name__)


class UserActorFollowService:
    """Own User Actor follower persistence, Accept delivery, and owner notification."""

    def __init__(
        self, *, database: object, fedify_gateway: object, bot: DiscordUserLookupPort
    ) -> None:
        """Bind persistence and outer delivery boundaries used by the lifecycle."""
        self.database = database
        self.fedify_gateway = fedify_gateway
        self.bot = bot

    async def handle_follow(
        self,
        *,
        target_actor_id: str,
        remote_actor_id: str,
        remote_inbox_url: str,
        remote_shared_inbox_url: str | None,
        follow_activity_id: str,
    ) -> object:
        """Persist one accepted relationship and send an idempotent signed Accept."""
        from .activitypub_handlers import HandlerResult

        user = self.database.users.get_user_by_actor_url(target_actor_id)
        if user is None:
            return HandlerResult(
                "skipped",
                InboundActivityOutcome.IGNORED_UNKNOWN_FOLLOW,
                "unknown local user actor",
            )
        # Endpoint admission happens before persistence and before the immediate
        # Accept side effect so the social graph never stores an SSRF-capable
        # remote inbox for later fanout or direct Accept delivery.
        try:
            admitted_inbox_url = admit_remote_delivery_endpoint(
                remote_inbox_url, local_origin=target_actor_id
            )
            admitted_shared_inbox_url = (
                None
                if remote_shared_inbox_url is None
                else admit_remote_delivery_endpoint(
                    remote_shared_inbox_url, local_origin=target_actor_id
                )
            )
        except RemoteDeliveryEndpointError as exc:
            return HandlerResult(
                "skipped",
                InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                str(exc),
            )

        # The immediate transaction makes ledger ownership and relationship refresh
        # one atomic decision under concurrent duplicate deliveries.
        try:
            with self.database.immediate_session() as session:
                transition = self.database.user_actor_followers.create_or_refresh(
                    session,
                    user_id=user.id,
                    remote_actor_id=remote_actor_id,
                    remote_inbox_url=admitted_inbox_url,
                    remote_shared_inbox_url=admitted_shared_inbox_url,
                    follow_activity_id=follow_activity_id,
                )
        except Exception as exc:
            from .db.repositories.user_actor_followers import FollowActivityIdCollision

            if isinstance(exc, FollowActivityIdCollision):
                logger.warning(
                    "Rejected reused Follow ID id=%s actor=%s target=%s",
                    follow_activity_id,
                    remote_actor_id,
                    target_actor_id,
                )
                return HandlerResult(
                    "skipped",
                    InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                    "Follow ID belongs to another relationship",
                )
            raise

        # Accept is outside the database transaction. A failed delivery leaves a
        # recoverable accepted row and pending notification for a duplicate Follow.
        await self.fedify_gateway.accept_user_actor_follow(
            actor_username=user.activitypub_username,
            actor_url=user.actor_url,
            remote_actor_id=remote_actor_id,
            remote_inbox_url=admitted_inbox_url,
            follow_activity_id=follow_activity_id,
        )
        claimed = self.database.user_actor_followers.claim_notification_attempt(
            transition.relationship_id
        )
        if claimed is not None:
            await self._notify_owner(user=user, remote_actor_id=remote_actor_id)
        return HandlerResult(
            "processed",
            InboundActivityOutcome.APPLIED,
            f"user actor follower {transition.transition}",
        )

    async def handle_unfollow(
        self,
        *,
        target_actor_id: str,
        remote_actor_id: str,
        follow_activity_id: str | None,
    ) -> object:
        """Remove the current relationship owned by the exact remote actor."""
        from .activitypub_handlers import HandlerResult

        del follow_activity_id
        user = self.database.users.get_user_by_actor_url(target_actor_id)
        if user is None:
            return HandlerResult(
                "skipped",
                InboundActivityOutcome.IGNORED_UNKNOWN_FOLLOW,
                "unknown local user actor",
            )
        with self.database.immediate_session() as session:
            removed = self.database.user_actor_followers.delete_relationship(
                session, user_id=user.id, remote_actor_id=remote_actor_id
            )
        if not removed:
            return HandlerResult(
                "skipped",
                InboundActivityOutcome.IGNORED_UNKNOWN_FOLLOW,
                "user actor follower not found",
            )
        return HandlerResult(
            "processed", InboundActivityOutcome.APPLIED, "user actor follower removed"
        )

    async def _notify_owner(self, *, user: object, remote_actor_id: str) -> None:
        """Attempt one non-retryable DM after the matching Accept succeeds."""
        try:
            discord_user = await self.bot.fetch_user(int(user.discord_user_id))
            remote_label = self._remote_label(remote_actor_id)
            local_host = urlparse(user.actor_url).netloc
            await discord_user.send(
                f"{remote_label} followed your Fediverse account "
                f"@{user.activitypub_username}@{local_host}."
            )
        except Exception:
            # The durable claim deliberately precedes Discord I/O, providing an
            # at-most-once attempt and preventing duplicate-Follow notification spam.
            logger.exception(
                "Could not notify owner of User Actor follower user_id=%s actor=%s",
                user.id,
                remote_actor_id,
            )

    @staticmethod
    def _remote_label(actor_id: str) -> str:
        """Build a bounded human-readable label without fetching remote profile data."""
        parsed = urlparse(actor_id)
        name = parsed.path.rstrip("/").split("/")[-1]
        return f"@{name}@{parsed.netloc}" if parsed.netloc and name else actor_id
