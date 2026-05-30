"""Operation layer for the `/ban-user` local-community moderation command."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..db import Database
from ..fediverse_identity import InvalidRemoteActorHandle, normalize_remote_actor_handle


@dataclass(slots=True)
class BanUserInput:
    """Carry one parsed `/ban-user` request from the Discord adapter."""

    database: Database
    settings: Settings
    discord_user_id: str
    community_slug: str
    actor_handle: str
    reason: str | None = None


@dataclass(slots=True)
class BanUserResult:
    """Report the visible command outcome and machine-readable reason."""

    applied: bool
    message: str
    reason: str


def ban_user_operation(operation_input: BanUserInput) -> BanUserResult:
    """Validate and persist one community-scoped local remote actor ban.

    The operation owns permission, slug lookup, handle normalization, duplicate
    semantics, and response text so the Discord adapter stays presentation-only.
    It never performs network resolution or sends federated moderation objects.
    """
    if operation_input.discord_user_id not in operation_input.settings.local_community_operator_allowlist:
        return BanUserResult(
            applied=False,
            message="You are not allowed to ban users from local communities with this bot.",
            reason="operator_not_allowlisted",
        )

    community_slug = operation_input.community_slug.strip()
    local_community = operation_input.database.local_communities.get_local_community_by_slug(
        community_slug
    )
    if local_community is None:
        return BanUserResult(
            applied=False,
            message=f"Unknown local community slug: {community_slug}",
            reason="unknown_community",
        )

    try:
        actor_handle = normalize_remote_actor_handle(operation_input.actor_handle)
    except InvalidRemoteActorHandle:
        return BanUserResult(
            applied=False,
            message="Invalid remote user handle. Use user@example.com.",
            reason="invalid_handle",
        )

    existing = operation_input.database.community_actor_bans.get_active_ban_by_handle(
        local_community_id=getattr(local_community, "id"),
        actor_handle=actor_handle,
    )
    if existing is not None:
        # Duplicate bans are rejected without changing the existing moderation
        # note; v1 has no implicit reason-edit semantics.
        reason_text = getattr(existing, "reason", None) or "not specified"
        return BanUserResult(
            applied=False,
            message=(
                f"User {actor_handle} is already banned in community {community_slug}.\n"
                f"Reason: {reason_text}"
            ),
            reason="duplicate_active_ban",
        )

    reason = operation_input.reason.strip() if operation_input.reason else None
    operation_input.database.community_actor_bans.create_active_ban(
        local_community_id=getattr(local_community, "id"),
        actor_handle=actor_handle,
        actor_url=None,
        created_by_discord_user_id=operation_input.discord_user_id,
        reason=reason,
    )
    return BanUserResult(
        applied=True,
        message=(
            f"Banned {actor_handle} from community {community_slug}.\n"
            f"Reason: {reason or 'not specified'}"
        ),
        reason="created",
    )
