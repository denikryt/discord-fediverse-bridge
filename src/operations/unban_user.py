"""Operation layer for removing community-scoped or global bridge bans."""

from __future__ import annotations

from dataclasses import dataclass, field

from discordops import Operation, Precondition

from ..config import Settings
from ..db import Database
from ..fediverse_identity import InvalidRemoteActorHandle, normalize_remote_actor_handle
from ..local_community_lifecycle import disabled_moderation_message, is_local_community_disabled
from ..local_community_permissions import can_access_local_community_from_guild, can_manage_local_community, is_super_admin
from ..models import CommunityActorBan, LocalCommunity


@dataclass(slots=True)
class UnbanUserInput:
    """Carry one `/unban-user` request plus memoized scope and ban state."""

    database: Database
    settings: Settings
    discord_user_id: str
    discord_guild_id: int | None
    community_slug: str | None
    actor_handle: str
    _community: LocalCommunity | None = field(default=None, init=False, repr=False)
    _community_loaded: bool = field(default=False, init=False, repr=False)
    _normalized_actor_handle: str | None = field(default=None, init=False, repr=False)
    _handle_loaded: bool = field(default=False, init=False, repr=False)
    _active_ban: CommunityActorBan | None = field(default=None, init=False, repr=False)
    _ban_loaded: bool = field(default=False, init=False, repr=False)

    @property
    def normalized_community_slug(self) -> str | None:
        """Return trimmed slug or None for global scope."""
        value = (self.community_slug or "").strip()
        return value or None

    @property
    def is_global(self) -> bool:
        """Return whether the command omitted community scope."""
        return self.normalized_community_slug is None

    def get_local_community(self) -> LocalCommunity | None:
        """Load selected community only for scoped requests."""
        if not self._community_loaded:
            slug = self.normalized_community_slug
            self._community = None if slug is None else self.database.local_communities.get_local_community_by_slug(slug)
            self._community_loaded = True
        return self._community

    def get_normalized_actor_handle(self) -> str | None:
        """Normalize the supplied local or remote handle without network I/O."""
        if not self._handle_loaded:
            try:
                self._normalized_actor_handle = normalize_remote_actor_handle(self.actor_handle)
            except InvalidRemoteActorHandle:
                self._normalized_actor_handle = None
            self._handle_loaded = True
        return self._normalized_actor_handle

    def get_active_ban(self) -> CommunityActorBan | None:
        """Load the active row in exactly the requested scope."""
        if not self._ban_loaded:
            handle = self.get_normalized_actor_handle()
            community = self.get_local_community()
            self._active_ban = None if handle is None else self.database.community_actor_bans.get_active_ban_by_handle(
                local_community_id=None if self.is_global else getattr(community, "id", None), actor_handle=handle
            )
            self._ban_loaded = True
        return self._active_ban


@dataclass(slots=True)
class UnbanUserResult:
    """Report visible unban outcome."""

    applied: bool
    message: str
    reason: str


def _global_authorized(value: UnbanUserInput) -> bool:
    """Allow global unban only for configured super-admins."""
    return not value.is_global or is_super_admin(settings=value.settings, discord_user_id=value.discord_user_id)


def _guild_context(value: UnbanUserInput) -> bool:
    """Require guild context only for community scope."""
    return value.is_global or value.discord_guild_id is not None


def _community_accessible(value: UnbanUserInput) -> bool:
    """Require an addressable community for scoped requests."""
    if value.is_global:
        return True
    community = value.get_local_community()
    return community is not None and can_access_local_community_from_guild(
        settings=value.settings, discord_user_id=value.discord_user_id,
        discord_guild_id=value.discord_guild_id, local_community=community, include_disabled=True,
    )


def _can_manage(value: UnbanUserInput) -> bool:
    """Require owner or super-admin for community scope."""
    if value.is_global:
        return True
    community = value.get_local_community()
    return community is not None and can_manage_local_community(
        settings=value.settings,
        discord_user_id=value.discord_user_id,
        local_community=community,
    )


def _active_community(value: UnbanUserInput) -> bool:
    """Require active lifecycle only for community-scoped moderation."""
    if value.is_global:
        return True
    community = value.get_local_community()
    return community is not None and not is_local_community_disabled(community)


def _valid_actor_handle(value: UnbanUserInput) -> bool:
    """Require a syntactically valid normalized local or remote handle."""
    return value.get_normalized_actor_handle() is not None


def _active_ban_exists(value: UnbanUserInput) -> bool:
    """Require one active ban in exactly the requested scope."""
    return value.get_active_ban() is not None


def _no_active_message(value: UnbanUserInput) -> str:
    """Render non-disclosing absence text for the requested scope."""
    handle = value.get_normalized_actor_handle() or value.actor_handle
    if value.is_global:
        return f"User {handle} is not actively banned from this bridge instance."
    return f"User {handle} is not actively banned in community {value.normalized_community_slug}."


class UnbanUserOperation(Operation):
    """Execute ordered scope authorization and atomic ban removal."""

    name = "unban_user"
    preconditions = (
        Precondition(
            name="not_super_admin",
            message="Only a super-admin can remove a global ban.",
            predicate=_global_authorized,
        ),
        Precondition(
            name="missing_guild_context",
            message="This command can only be used inside a guild.",
            predicate=_guild_context,
        ),
        Precondition(
            name="unknown_or_inaccessible_community",
            message=lambda value: (
                f"Unknown or inaccessible local community: "
                f"{value.normalized_community_slug}"
            ),
            predicate=_community_accessible,
        ),
        Precondition(
            name="cannot_manage_community",
            message="You are not allowed to manage this local community.",
            predicate=_can_manage,
        ),
        Precondition(
            name="community_disabled",
            message=lambda value: disabled_moderation_message(
                value.normalized_community_slug or ""
            ),
            predicate=_active_community,
        ),
        Precondition(
            name="invalid_handle",
            message="Invalid remote user handle. Use user@example.com.",
            predicate=_valid_actor_handle,
        ),
        Precondition(
            name="no_active_ban",
            message=_no_active_message,
            predicate=_active_ban_exists,
        ),
    )

    def reject(self, operation_input: UnbanUserInput, *, reason: str, message: str, **_: object) -> UnbanUserResult:
        """Return first failed precondition and preserve existing scoped audits."""
        if reason in {"cannot_manage_community", "community_disabled"}:
            community = operation_input.get_local_community()
            if community is not None:
                operation_input.database.management_audit.ban_remove_forbidden(
                    actor_discord_user_id=operation_input.discord_user_id,
                    community=community, failed_precondition=reason,
                )
        return UnbanUserResult(False, message, reason)

    def body(self, operation_input: UnbanUserInput) -> UnbanUserResult:
        """Deactivate one active scope-specific row and audit atomically."""
        handle = operation_input.get_normalized_actor_handle()
        community = operation_input.get_local_community()
        if handle is None or (not operation_input.is_global and community is None):
            return UnbanUserResult(False, "Unable to unban because the command state is invalid.", "invalid_operation_state")
        operation_input.database.management_actions.remove_ban(
            actor_discord_user_id=operation_input.discord_user_id,
            local_community_id=None if operation_input.is_global else community.id,
            actor_handle=handle,
        )
        scope = "this bridge instance" if operation_input.is_global else f"community {operation_input.normalized_community_slug}"
        return UnbanUserResult(True, f"Unbanned {handle} from {scope}.", "unbanned")



def unban_user_operation(operation_input: UnbanUserInput) -> UnbanUserResult:
    """Execute `/unban-user` through ordered preconditions."""
    return UnbanUserOperation().execute(operation_input)
