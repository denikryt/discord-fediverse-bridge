"""Command-side orchestration for local-community creation.

This operation layer keeps Discord parsing and permission checks out of the
domain service while avoiding policy drift inside the command adapter.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..db import Database
from ..local_communities.service import LocalCommunityError, LocalCommunityService


@dataclass(slots=True)
class CreateCommunityInput:
    """Carry one parsed `/create_community` request from Discord."""

    database: Database
    settings: Settings
    discord_user_id: str
    discord_guild_id: int
    discord_forum_channel_id: int
    slug: str
    name: str
    description: str | None


@dataclass(slots=True)
class CreateCommunityResult:
    """Report the observable result of one community-creation attempt."""

    applied: bool
    message: str
    reason: str


def create_community_authorization_precheck(operation_input: CreateCommunityInput) -> CreateCommunityResult | None:
    """Return a forbidden result before Discord placement when creation is unauthorized.

    The create-community modal needs the attempted slug for audit, but it must
    not create a Discord forum channel before authorization has been evaluated.
    This helper preserves the existing forbidden audit semantics while allowing
    the command adapter to order local validation before external side effects.
    """
    if operation_input.discord_user_id in operation_input.settings.local_community_operator_allowlist:
        return None
    operation_input.database.management_audit.community_create_forbidden(
        actor_discord_user_id=operation_input.discord_user_id,
        attempted_slug=operation_input.slug,
    )
    return CreateCommunityResult(
        applied=False,
        message="You are not allowed to create local communities with this bot.",
        reason="operator_not_allowlisted",
    )


def create_community_operation(operation_input: CreateCommunityInput) -> CreateCommunityResult:
    """Validate one local-community creation request and persist the result."""
    forbidden = create_community_authorization_precheck(operation_input)
    if forbidden is not None:
        return forbidden

    service = LocalCommunityService(
        database=operation_input.database,
        base_url=operation_input.settings.normalized_fedify_origin,
    )
    try:
        created = service.create_local_community(
            discord_guild_id=operation_input.discord_guild_id,
            discord_forum_channel_id=operation_input.discord_forum_channel_id,
            slug=operation_input.slug,
            name=operation_input.name,
            description=operation_input.description,
            created_by_discord_user_id=operation_input.discord_user_id,
        )
    except LocalCommunityError as exc:
        return CreateCommunityResult(
            applied=False,
            message=str(exc),
            reason="validation_failed",
        )

    return CreateCommunityResult(
        applied=True,
        message=(
            f"Created local community **{created.display_name}** "
            f"(`!{created.slug}@{created.actor_url.split('://', 1)[1].split('/', 1)[0]}`) "
            f"for forum channel <#{created.discord_forum_channel_id}>."
        ),
        reason="created",
    )
