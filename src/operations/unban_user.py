"""Operation layer for the `/unban-user` moderation command.

Unban is local-only bridge moderation. It deactivates an existing active ban row
and deliberately does not emit any ActivityPub moderation activity.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from discordops import Operation, Precondition

from ..config import Settings
from ..db import Database
from ..fediverse_identity import InvalidRemoteActorHandle, normalize_remote_actor_handle
from ..local_community_permissions import (
    can_access_local_community_from_guild,
    can_manage_local_community,
)
from ..models import CommunityActorBan, LocalCommunity


@dataclass(slots=True)
class UnbanUserInput:
    """Carry one parsed `/unban-user` request plus memoized derived state."""

    database: Database
    settings: Settings
    discord_user_id: str
    discord_guild_id: int | None
    community_slug: str
    actor_handle: str
    _community: LocalCommunity | None = field(default=None, init=False, repr=False)
    _community_loaded: bool = field(default=False, init=False, repr=False)
    _normalized_actor_handle: str | None = field(default=None, init=False, repr=False)
    _actor_handle_normalized: bool = field(default=False, init=False, repr=False)
    _actor_handle_error: InvalidRemoteActorHandle | None = field(default=None, init=False, repr=False)
    _active_ban: CommunityActorBan | None = field(default=None, init=False, repr=False)
    _active_ban_loaded: bool = field(default=False, init=False, repr=False)

    @property
    def normalized_community_slug(self) -> str:
        """Return the community slug after trimming Discord input whitespace."""
        return self.community_slug.strip()

    def get_local_community(self) -> LocalCommunity | None:
        """Load and memoize the target local community by slug."""
        if not self._community_loaded:
            self._community = self.database.local_communities.get_local_community_by_slug(
                self.normalized_community_slug
            )
            self._community_loaded = True
        return self._community

    def get_normalized_actor_handle(self) -> str | None:
        """Normalize and memoize the remote actor handle after authorization."""
        if not self._actor_handle_normalized:
            try:
                self._normalized_actor_handle = normalize_remote_actor_handle(self.actor_handle)
            except InvalidRemoteActorHandle as exc:
                self._actor_handle_error = exc
                self._normalized_actor_handle = None
            self._actor_handle_normalized = True
        return self._normalized_actor_handle

    def get_active_ban(self) -> CommunityActorBan | None:
        """Load and memoize the active ban row targeted by this unban."""
        if not self._active_ban_loaded:
            community = self.get_local_community()
            actor_handle = self.get_normalized_actor_handle()
            if community is None or actor_handle is None:
                self._active_ban = None
            else:
                self._active_ban = self.database.community_actor_bans.get_active_ban_by_handle(
                    local_community_id=community.id,
                    actor_handle=actor_handle,
                )
            self._active_ban_loaded = True
        return self._active_ban


@dataclass(slots=True)
class UnbanUserResult:
    """Report the visible `/unban-user` command outcome."""

    applied: bool
    message: str
    reason: str


def _has_guild_context(operation_input: UnbanUserInput) -> bool:
    """Return whether Discord supplied a guild id for this command."""
    return operation_input.discord_guild_id is not None


def _community_accessible(operation_input: UnbanUserInput) -> bool:
    """Return whether the slug exists and is reachable in this guild context."""
    community = operation_input.get_local_community()
    if community is None:
        return False
    return can_access_local_community_from_guild(
        settings=operation_input.settings,
        discord_user_id=operation_input.discord_user_id,
        discord_guild_id=operation_input.discord_guild_id,
        local_community=community,
    )


def _can_manage_community(operation_input: UnbanUserInput) -> bool:
    """Return whether the caller is owner or super-admin for the community."""
    community = operation_input.get_local_community()
    if community is None:
        return False
    return can_manage_local_community(
        settings=operation_input.settings,
        discord_user_id=operation_input.discord_user_id,
        local_community=community,
    )


def _valid_actor_handle(operation_input: UnbanUserInput) -> bool:
    """Return whether the target user argument is a normalized handle."""
    return operation_input.get_normalized_actor_handle() is not None


def _active_ban_exists(operation_input: UnbanUserInput) -> bool:
    """Return whether there is an active row to deactivate."""
    return operation_input.get_active_ban() is not None


def _inaccessible_message(operation_input: UnbanUserInput) -> str:
    """Build the shared inaccessible-community rejection text."""
    return f"Unknown or inaccessible local community: {operation_input.normalized_community_slug}"


def _no_active_ban_message(operation_input: UnbanUserInput) -> str:
    """Build the generic no-active-ban response without exposing history."""
    actor_handle = operation_input.get_normalized_actor_handle() or operation_input.actor_handle
    return (
        f"User {actor_handle} is not actively banned in community "
        f"{operation_input.normalized_community_slug}."
    )


class UnbanUserOperation(Operation):
    """Declarative operation for removing one active community-scoped ban."""

    name = "unban_user"
    _REJECTION_REASONS = {
        "guild_context": "missing_guild_context",
        "community_accessible": "unknown_or_inaccessible_community",
        "can_manage_community": "cannot_manage_community",
        "valid_actor_handle": "invalid_handle",
        "active_ban_exists": "no_active_ban",
    }
    preconditions = (
        Precondition(
            name="guild_context",
            message="This command can only be used inside a guild.",
            predicate=_has_guild_context,
        ),
        Precondition(
            name="community_accessible",
            message=_inaccessible_message,
            predicate=_community_accessible,
        ),
        Precondition(
            name="can_manage_community",
            message="You are not allowed to manage this local community.",
            predicate=_can_manage_community,
        ),
        Precondition(
            name="valid_actor_handle",
            message="Invalid remote user handle. Use user@example.com.",
            predicate=_valid_actor_handle,
        ),
        Precondition(
            name="active_ban_exists",
            message=_no_active_ban_message,
            predicate=_active_ban_exists,
        ),
    )

    def reject(
        self,
        operation_input: UnbanUserInput,
        *,
        reason: str,
        message: str,
        **_: object,
    ) -> UnbanUserResult:
        """Return a rejected command result for the first failed precondition."""
        return UnbanUserResult(
            applied=False,
            message=message,
            reason=self._REJECTION_REASONS.get(reason, reason),
        )

    def body(self, operation_input: UnbanUserInput) -> UnbanUserResult:
        """Deactivate the active ban after all command checks pass."""
        community = operation_input.get_local_community()
        actor_handle = operation_input.get_normalized_actor_handle()
        if community is None or actor_handle is None:
            # Preconditions guarantee these are present. This branch protects
            # future direct callers from accidentally mutating invalid state.
            return UnbanUserResult(
                applied=False,
                message="Unable to unban because the command state is invalid.",
                reason="invalid_operation_state",
            )

        operation_input.database.community_actor_bans.deactivate_active_ban_by_handle(
            local_community_id=community.id,
            actor_handle=actor_handle,
        )
        return UnbanUserResult(
            applied=True,
            message=f"Unbanned {actor_handle} from community {operation_input.normalized_community_slug}.",
            reason="unbanned",
        )


def unban_user_operation(operation_input: UnbanUserInput) -> UnbanUserResult:
    """Execute `/unban-user` through ordered `discordops` preconditions."""
    return UnbanUserOperation().execute(operation_input)
