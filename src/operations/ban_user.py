"""Operation layer for the `/ban-user` local-community moderation command.

The command is implemented as an ordered `discordops` operation because the
precondition order is part of the security contract: callers must first be able
to address an active community from the command guild, then be allowed to manage
it, before handle validation or duplicate-ban checks can reveal extra state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from discordops import Operation, Precondition

from ..config import Settings
from ..db import Database
from ..fediverse_identity import InvalidRemoteActorHandle, normalize_remote_actor_handle
from ..local_community_lifecycle import disabled_moderation_message, is_local_community_disabled
from ..management_audit import (
    ACTION_BAN_CREATE_FORBIDDEN,
    REASON_COMMUNITY_DISABLED,
    REASON_NOT_OWNER_OR_SUPER_ADMIN,
    RESULT_FORBIDDEN,
    TARGET_REMOTE_ACTOR,
)
from ..local_community_permissions import (
    can_access_local_community_from_guild,
    can_manage_local_community,
)
from ..models import LocalCommunity


@dataclass(slots=True)
class BanUserInput:
    """Carry one parsed `/ban-user` request plus cached derived state."""

    database: Database
    settings: Settings
    discord_user_id: str
    discord_guild_id: int | None
    community_slug: str
    actor_handle: str
    reason: str | None = None
    _community: LocalCommunity | None = field(default=None, init=False, repr=False)
    _community_loaded: bool = field(default=False, init=False, repr=False)
    _normalized_actor_handle: str | None = field(default=None, init=False, repr=False)
    _actor_handle_normalized: bool = field(default=False, init=False, repr=False)
    _actor_handle_error: InvalidRemoteActorHandle | None = field(default=None, init=False, repr=False)
    _existing_ban: object | None = field(default=None, init=False, repr=False)
    _existing_ban_loaded: bool = field(default=False, init=False, repr=False)

    @property
    def normalized_community_slug(self) -> str:
        """Return the command slug after trimming Discord input whitespace."""
        return self.community_slug.strip()

    def get_local_community(self) -> LocalCommunity | None:
        """Load and memoize the target active local community by slug."""
        # Several preconditions need the same row. Memoizing preserves the
        # observable lookup order without repeating database reads.
        if not self._community_loaded:
            self._community = self.database.local_communities.get_local_community_by_slug(
                self.normalized_community_slug
            )
            self._community_loaded = True
        return self._community

    def get_normalized_actor_handle(self) -> str | None:
        """Normalize and memoize the remote actor handle, or return None."""
        # Normalization is delayed until after community access and management
        # checks. This prevents unauthorized callers from learning whether their
        # handle input is valid.
        if not self._actor_handle_normalized:
            try:
                self._normalized_actor_handle = normalize_remote_actor_handle(self.actor_handle)
            except InvalidRemoteActorHandle as exc:
                self._actor_handle_error = exc
                self._normalized_actor_handle = None
            self._actor_handle_normalized = True
        return self._normalized_actor_handle

    def get_existing_active_ban(self) -> object | None:
        """Load and memoize the duplicate active ban row for valid requests."""
        # Duplicate lookup is intentionally after authorization and handle
        # validation so moderation state is not exposed to unrelated callers.
        if not self._existing_ban_loaded:
            community = self.get_local_community()
            actor_handle = self.get_normalized_actor_handle()
            if community is None or actor_handle is None:
                self._existing_ban = None
            else:
                self._existing_ban = self.database.community_actor_bans.get_active_ban_by_handle(
                    local_community_id=community.id,
                    actor_handle=actor_handle,
                )
            self._existing_ban_loaded = True
        return self._existing_ban


@dataclass(slots=True)
class BanUserResult:
    """Report the visible command outcome and machine-readable reason."""

    applied: bool
    message: str
    reason: str


def _has_guild_context(operation_input: BanUserInput) -> bool:
    """Return whether Discord supplied a guild id for this command."""
    return operation_input.discord_guild_id is not None


def _community_accessible(operation_input: BanUserInput) -> bool:
    """Return whether the caller may address this community from the guild."""
    community = operation_input.get_local_community()
    if community is None:
        return False
    return can_access_local_community_from_guild(
        settings=operation_input.settings,
        discord_user_id=operation_input.discord_user_id,
        discord_guild_id=operation_input.discord_guild_id,
        local_community=community,
        include_disabled=True,
    )


def _can_manage_community(operation_input: BanUserInput) -> bool:
    """Return whether the caller is owner or super-admin for the community."""
    community = operation_input.get_local_community()
    if community is None:
        return False
    return can_manage_local_community(
        settings=operation_input.settings,
        discord_user_id=operation_input.discord_user_id,
        local_community=community,
    )


def _valid_actor_handle(operation_input: BanUserInput) -> bool:
    """Return whether the remote actor handle matches the v1 command format."""
    return operation_input.get_normalized_actor_handle() is not None


def _no_duplicate_active_ban(operation_input: BanUserInput) -> bool:
    """Return whether no active ban exists for the same community and actor."""
    return operation_input.get_existing_active_ban() is None


def _community_active(operation_input: BanUserInput) -> bool:
    """Return whether moderation/list operations may act on this community."""
    community = operation_input.get_local_community()
    return community is not None and not is_local_community_disabled(community)


def _disabled_message(operation_input: BanUserInput) -> str:
    """Build the shared disabled-community moderation rejection text."""
    return disabled_moderation_message(operation_input.normalized_community_slug)


def _inaccessible_message(operation_input: BanUserInput) -> str:
    """Build the shared inaccessible-community rejection text."""
    return f"Unknown or inaccessible local community: {operation_input.normalized_community_slug}"


def _duplicate_active_ban_message(operation_input: BanUserInput) -> str:
    """Build the duplicate-ban rejection message using the stored reason."""
    existing = operation_input.get_existing_active_ban()
    actor_handle = operation_input.get_normalized_actor_handle() or operation_input.actor_handle
    reason_text = getattr(existing, "reason", None) or "not specified"
    return (
        f"User {actor_handle} is already banned in community {operation_input.normalized_community_slug}.\n"
        f"Reason: {reason_text}"
    )


class BanUserOperation(Operation):
    """Declarative operation for one community-scoped local actor ban."""

    name = "ban_user"
    _REJECTION_REASONS = {
        "guild_context": "missing_guild_context",
        "community_accessible": "unknown_or_inaccessible_community",
        "can_manage_community": "cannot_manage_community",
        "community_active": "community_disabled",
        "valid_actor_handle": "invalid_handle",
        "no_duplicate_active_ban": "duplicate_active_ban",
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
            name="community_active",
            message=_disabled_message,
            predicate=_community_active,
        ),
        Precondition(
            name="valid_actor_handle",
            message="Invalid remote user handle. Use user@example.com.",
            predicate=_valid_actor_handle,
        ),
        Precondition(
            name="no_duplicate_active_ban",
            message=_duplicate_active_ban_message,
            predicate=_no_duplicate_active_ban,
        ),
    )

    def reject(
        self,
        operation_input: BanUserInput,
        *,
        reason: str,
        message: str,
        **_: object,
    ) -> BanUserResult:
        """Return one rejected command result for the first failed precondition."""
        # discordops exposes the failed precondition name; command callers keep
        # stable reason codes from the ban-user operation contract.
        if reason in {"can_manage_community", "community_active"}:
            community = operation_input.get_local_community()
            if community is not None:
                operation_input.database.management_audit_events.create_event(
                    action=ACTION_BAN_CREATE_FORBIDDEN,
                    result=RESULT_FORBIDDEN,
                    actor_discord_user_id=operation_input.discord_user_id,
                    local_community_id=community.id,
                    target_type=TARGET_REMOTE_ACTOR,
                    target_id=None,
                    reason_code=(
                        REASON_NOT_OWNER_OR_SUPER_ADMIN
                        if reason == "can_manage_community"
                        else REASON_COMMUNITY_DISABLED
                    ),
                )
        return BanUserResult(
            applied=False,
            message=message,
            reason=self._REJECTION_REASONS.get(reason, reason),
        )

    def body(self, operation_input: BanUserInput) -> BanUserResult:
        """Persist the active ban after all ordered preconditions have passed."""
        community = operation_input.get_local_community()
        actor_handle = operation_input.get_normalized_actor_handle()
        if community is None or actor_handle is None:
            # This defensive branch should be unreachable because preconditions
            # already established both facts. Keeping it explicit prevents a
            # malformed future caller from creating an orphan moderation row.
            return BanUserResult(
                applied=False,
                message="Unable to create ban because the command state is invalid.",
                reason="invalid_operation_state",
            )

        reason = operation_input.reason.strip() if operation_input.reason else None
        operation_input.database.community_actor_bans.create_active_ban_with_audit(
            local_community_id=community.id,
            actor_handle=actor_handle,
            actor_url=None,
            created_by_discord_user_id=operation_input.discord_user_id,
            reason=reason,
            audit_repository=operation_input.database.management_audit_events,
        )
        return BanUserResult(
            applied=True,
            message=(
                f"Banned {actor_handle} from community {operation_input.normalized_community_slug}.\n"
                f"Reason: {reason or 'not specified'}"
            ),
            reason="created",
        )


def ban_user_operation(operation_input: BanUserInput) -> BanUserResult:
    """Execute the `/ban-user` operation through ordered `discordops` checks."""
    return BanUserOperation().execute(operation_input)
