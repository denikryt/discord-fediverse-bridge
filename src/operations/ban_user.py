"""Operation layer for the `/ban-user` local-community moderation command.

The command is implemented as an ordered `discordops` operation because the
precondition order is part of the security contract: once a community exists,
callers who cannot manage it must be rejected before handle validation or
moderation-state duplicate checks reveal extra information.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from discordops import Operation, Precondition

from ..config import Settings
from ..db import Database
from ..fediverse_identity import InvalidRemoteActorHandle, normalize_remote_actor_handle
from ..local_community_permissions import can_manage_local_community


@dataclass(slots=True)
class BanUserInput:
    """Carry one parsed `/ban-user` request plus cached derived state."""

    database: Database
    settings: Settings
    discord_user_id: str
    community_slug: str
    actor_handle: str
    reason: str | None = None
    _community: object | None = field(default=None, init=False, repr=False)
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

    def get_local_community(self) -> object | None:
        """Load and memoize the target local community by slug."""
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
        # Normalization is delayed until after authorization. This prevents
        # unauthorized callers from learning whether their handle input is valid.
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
                    local_community_id=getattr(community, "id"),
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


def _community_exists(operation_input: BanUserInput) -> bool:
    """Return whether the requested local community slug exists."""
    return operation_input.get_local_community() is not None


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
        "community_exists": "unknown_community",
        "can_manage_community": "cannot_manage_community",
        "valid_actor_handle": "invalid_handle",
        "no_duplicate_active_ban": "duplicate_active_ban",
    }
    preconditions = (
        Precondition(
            name="community_exists",
            message=lambda op: f"Unknown local community slug: {op.normalized_community_slug}",
            predicate=_community_exists,
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
        # the historical reason codes from the ban-user operation contract.
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
        operation_input.database.community_actor_bans.create_active_ban(
            local_community_id=getattr(community, "id"),
            actor_handle=actor_handle,
            actor_url=None,
            created_by_discord_user_id=operation_input.discord_user_id,
            reason=reason,
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
