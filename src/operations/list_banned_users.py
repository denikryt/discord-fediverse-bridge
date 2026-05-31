"""Operation layer for listing active local-community actor bans."""

from __future__ import annotations

from dataclasses import dataclass, field

from discordops import Operation, Precondition

from ..config import Settings
from ..db import Database
from ..local_community_permissions import can_access_local_community_from_guild
from ..models import LocalCommunity


@dataclass(slots=True)
class ListBannedUsersInput:
    """Carry one `/list-banned-users` request and cached community state."""

    database: Database
    settings: Settings
    discord_user_id: str
    discord_guild_id: int | None
    community_slug: str
    limit: int = 20
    _community: LocalCommunity | None = field(default=None, init=False, repr=False)
    _community_loaded: bool = field(default=False, init=False, repr=False)

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


@dataclass(slots=True)
class ListBannedUsersResult:
    """Report the visible `/list-banned-users` command outcome."""

    applied: bool
    message: str
    reason: str


def _has_guild_context(operation_input: ListBannedUsersInput) -> bool:
    """Return whether Discord supplied a guild id for this command."""
    return operation_input.discord_guild_id is not None


def _community_accessible(operation_input: ListBannedUsersInput) -> bool:
    """Return whether the selected community can be listed from this guild."""
    community = operation_input.get_local_community()
    if community is None:
        return False
    return can_access_local_community_from_guild(
        settings=operation_input.settings,
        discord_user_id=operation_input.discord_user_id,
        discord_guild_id=operation_input.discord_guild_id,
        local_community=community,
    )


def _inaccessible_message(operation_input: ListBannedUsersInput) -> str:
    """Build the shared inaccessible-community rejection text."""
    return f"Unknown or inaccessible local community: {operation_input.normalized_community_slug}"


def _format_reason(reason: str | None) -> str:
    """Return compact reason text for Discord-visible list output."""
    if not reason:
        return "reason not specified"
    # Keep each line compact so the 20-row v1 output is unlikely to exceed
    # Discord's message limit while preserving the stored DB reason unchanged.
    if len(reason) > 160:
        return f"{reason[:157]}..."
    return reason


class ListBannedUsersOperation(Operation):
    """Declarative operation for listing active bans in one community."""

    name = "list_banned_users"
    _REJECTION_REASONS = {
        "guild_context": "missing_guild_context",
        "community_accessible": "unknown_or_inaccessible_community",
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
    )

    def reject(
        self,
        operation_input: ListBannedUsersInput,
        *,
        reason: str,
        message: str,
        **_: object,
    ) -> ListBannedUsersResult:
        """Return a rejected command result for the first failed precondition."""
        return ListBannedUsersResult(
            applied=False,
            message=message,
            reason=self._REJECTION_REASONS.get(reason, reason),
        )

    def body(self, operation_input: ListBannedUsersInput) -> ListBannedUsersResult:
        """Build the ephemeral active-ban list for one accessible community."""
        community = operation_input.get_local_community()
        if community is None:
            # Preconditions guarantee accessibility. This branch prevents future
            # direct callers from accidentally formatting a global list.
            return ListBannedUsersResult(
                applied=False,
                message=_inaccessible_message(operation_input),
                reason="unknown_or_inaccessible_community",
            )

        total = operation_input.database.community_actor_bans.count_active_bans_for_community(
            local_community_id=community.id,
        )
        if total == 0:
            return ListBannedUsersResult(
                applied=True,
                message=f"Community {operation_input.normalized_community_slug} has no active bans.",
                reason="empty",
            )

        visible_limit = max(1, operation_input.limit)
        bans = operation_input.database.community_actor_bans.list_active_bans_for_community(
            local_community_id=community.id,
            limit=visible_limit,
        )
        lines = [f"Banned users in community {operation_input.normalized_community_slug}:"]
        lines.extend(f"- {ban.actor_handle} — {_format_reason(ban.reason)}" for ban in bans)
        if total > visible_limit:
            lines.append(f"Showing {visible_limit} of {total} active bans.")
        return ListBannedUsersResult(
            applied=True,
            message="\n".join(lines),
            reason="listed",
        )


def list_banned_users_operation(operation_input: ListBannedUsersInput) -> ListBannedUsersResult:
    """Execute `/list-banned-users` through ordered preconditions."""
    return ListBannedUsersOperation().execute(operation_input)
