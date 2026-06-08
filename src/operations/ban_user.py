"""Operation layer for community-scoped and global bridge user bans."""

from __future__ import annotations

from dataclasses import dataclass, field

from discordops import Operation, Precondition

from ..config import Settings
from ..db import Database
from ..local_community_lifecycle import disabled_moderation_message, is_local_community_disabled
from ..local_community_permissions import can_access_local_community_from_guild, can_manage_local_community, is_super_admin
from ..models import LocalCommunity
from ..user_bans import ResolvedBanTarget, UnknownLocalBanTarget, resolve_ban_target


@dataclass(slots=True)
class BanUserInput:
    """Carry one `/ban-user` request and memoized authorization/identity state."""

    database: Database
    settings: Settings
    discord_user_id: str
    discord_guild_id: int | None
    community_slug: str | None
    actor_handle: str
    reason: str | None = None
    _community: LocalCommunity | None = field(default=None, init=False, repr=False)
    _community_loaded: bool = field(default=False, init=False, repr=False)
    _target: ResolvedBanTarget | None = field(default=None, init=False, repr=False)
    _target_error: Exception | None = field(default=None, init=False, repr=False)
    _target_loaded: bool = field(default=False, init=False, repr=False)
    _existing: object | None = field(default=None, init=False, repr=False)
    _existing_loaded: bool = field(default=False, init=False, repr=False)

    @property
    def normalized_community_slug(self) -> str | None:
        """Return trimmed community slug or None for global scope."""
        value = (self.community_slug or "").strip()
        return value or None

    @property
    def is_global(self) -> bool:
        """Return whether the command omitted community scope."""
        return self.normalized_community_slug is None

    def get_local_community(self) -> LocalCommunity | None:
        """Load the selected local community only for scoped requests."""
        if not self._community_loaded:
            slug = self.normalized_community_slug
            self._community = None if slug is None else self.database.local_communities.get_local_community_by_slug(slug)
            self._community_loaded = True
        return self._community

    def get_target(self) -> ResolvedBanTarget | None:
        """Resolve local/remote target after authorization has allowed validation."""
        if not self._target_loaded:
            try:
                self._target = resolve_ban_target(database=self.database, settings=self.settings, value=self.actor_handle)
            except Exception as exc:
                self._target_error = exc
                self._target = None
            self._target_loaded = True
        return self._target

    def get_existing_active_ban(self) -> object | None:
        """Load duplicate state in exactly the requested scope."""
        if not self._existing_loaded:
            target = self.get_target()
            community = self.get_local_community()
            self._existing = None if target is None else self.database.community_actor_bans.get_active_ban_by_handle(
                local_community_id=None if self.is_global else getattr(community, "id", None),
                actor_handle=target.actor_handle,
            )
            self._existing_loaded = True
        return self._existing


@dataclass(slots=True)
class BanUserResult:
    """Report command outcome plus notification metadata for the adapter."""

    applied: bool
    message: str
    reason: str
    activation_kind: str | None = None
    target_discord_user_id: str | None = None
    scope: str | None = None
    community_slug: str | None = None
    stored_reason: str | None = None


def _is_global_scope_authorized(value: BanUserInput) -> bool:
    """Reject omitted-community calls before target validation unless super-admin."""
    return not value.is_global or is_super_admin(settings=value.settings, discord_user_id=value.discord_user_id)


def _has_required_guild_context(value: BanUserInput) -> bool:
    """Require guild context only for community-owner scoped operations."""
    return value.is_global or value.discord_guild_id is not None


def _is_community_accessible(value: BanUserInput) -> bool:
    """Resolve and authorize the selected community, while global scope skips it."""
    if value.is_global:
        return True
    community = value.get_local_community()
    return community is not None and can_access_local_community_from_guild(
        settings=value.settings, discord_user_id=value.discord_user_id,
        discord_guild_id=value.discord_guild_id, local_community=community, include_disabled=True,
    )


def _can_manage_community(value: BanUserInput) -> bool:
    """Require owner or super-admin for selected community."""
    if value.is_global:
        return True
    community = value.get_local_community()
    return community is not None and can_manage_local_community(
        settings=value.settings, discord_user_id=value.discord_user_id, local_community=community
    )


def _is_community_moderation_enabled(value: BanUserInput) -> bool:
    """Require active lifecycle only for community scope."""
    return value.is_global or (value.get_local_community() is not None and not is_local_community_disabled(value.get_local_community()))


def _is_ban_target_valid(value: BanUserInput) -> bool:
    """Return whether syntax and local-domain DB resolution succeeded."""
    return value.get_target() is not None


def _target_error(value: BanUserInput) -> str:
    """Return precise local lookup validation or generic handle syntax text."""
    if isinstance(value._target_error, UnknownLocalBanTarget):
        return str(value._target_error)
    return "Invalid remote user handle. Use user@example.com."


def _has_no_active_duplicate_ban(value: BanUserInput) -> bool:
    """Return whether no active row exists in the requested scope."""
    return value.get_existing_active_ban() is None


def _duplicate_message(value: BanUserInput) -> str:
    """Render private duplicate details for the authorized moderator."""
    target = value.get_target()
    existing = value.get_existing_active_ban()
    if value.is_global:
        return f"User {target.actor_handle if target else value.actor_handle} is already banned from this bridge instance.\nReason: {getattr(existing, 'reason', None) or 'not specified'}"
    return f"User {target.actor_handle if target else value.actor_handle} is already banned in community {value.normalized_community_slug}.\nReason: {getattr(existing, 'reason', None) or 'not specified'}"


class BanUserOperation(Operation):
    """Execute ordered authorization, identity resolution, and atomic mutation."""

    name = "ban_user"
    preconditions = (
        Precondition(
            name="global_scope_requires_super_admin",
            message="Only a super-admin can create a global ban.",
            predicate=_is_global_scope_authorized,
        ),
        Precondition(
            name="missing_guild_context",
            message="This command can only be used inside a guild.",
            predicate=_has_required_guild_context,
        ),
        Precondition(
            name="unknown_or_inaccessible_community",
            message=lambda value: (
                f"Unknown or inaccessible local community: "
                f"{value.normalized_community_slug}"
            ),
            predicate=_is_community_accessible,
        ),
        Precondition(
            name="cannot_manage_community",
            message="You are not allowed to manage this local community.",
            predicate=_can_manage_community,
        ),
        Precondition(
            name="community_disabled",
            message=lambda value: disabled_moderation_message(
                value.normalized_community_slug or ""
            ),
            predicate=_is_community_moderation_enabled,
        ),
        Precondition(
            name="invalid_handle",
            message=_target_error,
            predicate=_is_ban_target_valid,
        ),
        Precondition(
            name="duplicate_active_ban",
            message=_duplicate_message,
            predicate=_has_no_active_duplicate_ban,
        ),
    )

    def reject(self, operation_input: BanUserInput, *, reason: str, message: str, **_: object) -> BanUserResult:
        """Return first failure and audit only defined authorization denials."""
        if reason == "global_scope_requires_super_admin":
            operation_input.database.management_audit.ban_create_global_forbidden(
                actor_discord_user_id=operation_input.discord_user_id
            )
        elif reason in {"cannot_manage_community", "community_disabled"}:
            community = operation_input.get_local_community()
            if community is not None:
                operation_input.database.management_audit.ban_create_forbidden(
                    actor_discord_user_id=operation_input.discord_user_id,
                    community=community, failed_precondition=reason,
                )
        return BanUserResult(False, message, reason)

    def body(self, operation_input: BanUserInput) -> BanUserResult:
        """Persist ban and audit atomically after all checks pass."""
        target = operation_input.get_target()
        community = operation_input.get_local_community()
        if target is None or (not operation_input.is_global and community is None):
            return BanUserResult(False, "Unable to create ban because the command state is invalid.", "invalid_operation_state")
        stored_reason = operation_input.reason.strip() if operation_input.reason and operation_input.reason.strip() else None
        mutation_kwargs = {
            "actor_discord_user_id": operation_input.discord_user_id,
            "local_community_id": None if operation_input.is_global else community.id,
            "actor_handle": target.actor_handle,
            "actor_url": target.actor_url,
            "reason": stored_reason,
        }
        # Preserve the legacy remote-target call shape while attaching immutable
        # Discord identity only for locally registered users.
        if target.discord_user_id is not None:
            mutation_kwargs["target_discord_user_id"] = target.discord_user_id
        activation = operation_input.database.management_actions.create_or_reactivate_ban(**mutation_kwargs)
        scope_text = "this bridge instance" if operation_input.is_global else f"community {operation_input.normalized_community_slug}"
        return BanUserResult(
            True, f"Banned {target.actor_handle} from {scope_text}.\nReason: {stored_reason or 'not specified'}",
            activation.kind, activation_kind=activation.kind,
            target_discord_user_id=target.discord_user_id,
            scope="global" if operation_input.is_global else "community",
            community_slug=operation_input.normalized_community_slug,
            stored_reason=stored_reason,
        )


def ban_user_operation(operation_input: BanUserInput) -> BanUserResult:
    """Execute `/ban-user` through ordered discordops preconditions."""
    return BanUserOperation().execute(operation_input)
