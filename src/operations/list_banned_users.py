"""Operation layer for listing active community or global bans."""

from __future__ import annotations

from dataclasses import dataclass, field

from discordops import Operation, Precondition

from ..config import Settings
from ..db import Database
from ..models import LocalCommunity
from .common_preconditions import (
    SCOPED_GUILD_CONTEXT_REQUIRED,
    SCOPED_LOCAL_COMMUNITY_ACCESSIBLE,
    SCOPED_LOCAL_COMMUNITY_MODERATION_ENABLED,
    global_scope_authorized_precondition,
)


@dataclass(slots=True)
class ListBannedUsersInput:
    """Carry one list request and memoized scope state."""

    database: Database
    settings: Settings
    discord_user_id: str
    discord_guild_id: int | None
    community_slug: str | None
    limit: int = 20
    _community: LocalCommunity | None = field(default=None, init=False, repr=False)
    _loaded: bool = field(default=False, init=False, repr=False)

    @property
    def normalized_community_slug(self) -> str | None:
        """Return trimmed slug or None for global list."""
        value = (self.community_slug or "").strip()
        return value or None

    @property
    def is_global(self) -> bool:
        """Return whether global scope was requested."""
        return self.normalized_community_slug is None

    def get_local_community(self) -> LocalCommunity | None:
        """Load selected community only when present."""
        if not self._loaded:
            slug = self.normalized_community_slug
            self._community = None if slug is None else self.database.local_communities.get_local_community_by_slug(slug)
            self._loaded = True
        return self._community


@dataclass(slots=True)
class ListBannedUsersResult:
    """Report visible list outcome."""

    applied: bool
    message: str
    reason: str


def _format_reason(reason: str | None) -> str:
    """Return compact reason text without modifying stored data."""
    if not reason:
        return "reason not specified"
    return reason if len(reason) <= 160 else f"{reason[:157]}..."



class ListBannedUsersOperation(Operation):
    """Authorize and render active bans for one explicit scope."""

    name = "list_banned_users"
    preconditions = (
        global_scope_authorized_precondition(
            message="Only a super-admin can list global bans.",
        ),
        SCOPED_GUILD_CONTEXT_REQUIRED,
        SCOPED_LOCAL_COMMUNITY_ACCESSIBLE,
        SCOPED_LOCAL_COMMUNITY_MODERATION_ENABLED,
    )

    def reject(self, operation_input: ListBannedUsersInput, *, reason: str, message: str, **_: object) -> ListBannedUsersResult:
        """Return the first authorization or lifecycle rejection."""
        return ListBannedUsersResult(False, message, reason)

    def body(self, operation_input: ListBannedUsersInput) -> ListBannedUsersResult:
        """Render active rows for global or selected community scope."""
        limit = max(1, operation_input.limit)
        if operation_input.is_global:
            total = operation_input.database.community_actor_bans.count_active_global_bans()
            rows = operation_input.database.community_actor_bans.list_active_global_bans(limit=limit)
            heading = "Globally banned users:"
            empty = "This bridge instance has no active global bans."
        else:
            community = operation_input.get_local_community()
            total = operation_input.database.community_actor_bans.count_active_bans_for_community(local_community_id=community.id)
            rows = operation_input.database.community_actor_bans.list_active_bans_for_community(local_community_id=community.id, limit=limit)
            heading = f"Banned users in community {operation_input.normalized_community_slug}:"
            empty = f"Community {operation_input.normalized_community_slug} has no active bans."
        if total == 0:
            return ListBannedUsersResult(True, empty, "empty")
        lines = [heading, *(f"- {row.actor_handle} — {_format_reason(row.reason)}" for row in rows)]
        if total > limit:
            lines.append(f"Showing {limit} of {total} active bans.")
        return ListBannedUsersResult(True, "\n".join(lines), "listed")


def list_banned_users_operation(operation_input: ListBannedUsersInput) -> ListBannedUsersResult:
    """Execute `/list-banned-users` through ordered preconditions."""
    return ListBannedUsersOperation().execute(operation_input)
