"""Operation contract for editing local-community display metadata.

The Discord modal adapter owns UI concerns, while this module owns the runtime
security boundary and persistence behavior for `/edit-community` submissions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from discordops import Operation, Precondition

from ..config import Settings
from ..db import Database
from ..local_communities.service import LocalCommunityError, normalize_display_name, normalize_summary
from ..local_community_lifecycle import normalize_local_community_status
from ..local_community_permissions import can_access_local_community_from_guild, can_manage_local_community
from ..models import LocalCommunity


@dataclass(slots=True)
class EditCommunityInput:
    """Carry one modal-submit request for local-community metadata editing."""

    database: Database
    settings: Settings
    discord_user_id: str
    discord_guild_id: int | None
    community_slug: str
    display_name: str
    summary: str | None
    status: str = "active"
    _community: LocalCommunity | None = field(default=None, init=False, repr=False)
    _community_loaded: bool = field(default=False, init=False, repr=False)
    _normalized_display_name: str | None = field(default=None, init=False, repr=False)
    _display_name_error: LocalCommunityError | None = field(default=None, init=False, repr=False)
    _display_name_loaded: bool = field(default=False, init=False, repr=False)
    _normalized_summary: str | None = field(default=None, init=False, repr=False)
    _summary_error: LocalCommunityError | None = field(default=None, init=False, repr=False)
    _summary_loaded: bool = field(default=False, init=False, repr=False)
    _normalized_status: str | None = field(default=None, init=False, repr=False)
    _status_loaded: bool = field(default=False, init=False, repr=False)

    @property
    def normalized_community_slug(self) -> str:
        """Return the trimmed community slug from the Discord command value."""
        return self.community_slug.strip()

    def get_local_community(self) -> LocalCommunity | None:
        """Load and memoize the target local community by globally unique slug."""
        if not self._community_loaded:
            self._community = self.database.local_communities.get_local_community_by_slug(
                self.normalized_community_slug
            )
            self._community_loaded = True
        return self._community

    def get_normalized_display_name(self) -> str | None:
        """Return validated display-name text or remember the validation error."""
        if not self._display_name_loaded:
            try:
                self._normalized_display_name = normalize_display_name(self.display_name)
            except LocalCommunityError as exc:
                self._display_name_error = exc
                self._normalized_display_name = None
            self._display_name_loaded = True
        return self._normalized_display_name

    def get_normalized_summary(self) -> str | None:
        """Return normalized optional summary text or remember validation errors."""
        if not self._summary_loaded:
            try:
                self._normalized_summary = normalize_summary(self.summary)
            except LocalCommunityError as exc:
                self._summary_error = exc
                self._normalized_summary = None
            self._summary_loaded = True
        return self._normalized_summary

    def get_normalized_status(self) -> str | None:
        """Return the validated lifecycle status submitted by the modal."""
        if not self._status_loaded:
            self._normalized_status = normalize_local_community_status(self.status)
            self._status_loaded = True
        return self._normalized_status


@dataclass(slots=True)
class EditCommunityResult:
    """Report the visible result of one `/edit-community` modal submit."""

    applied: bool
    message: str
    reason: str


def _has_guild_context(operation_input: EditCommunityInput) -> bool:
    """Return whether Discord supplied a guild id for this command."""
    return operation_input.discord_guild_id is not None


def _community_accessible(operation_input: EditCommunityInput) -> bool:
    """Return whether the requested community exists in the caller's scope."""
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


def _can_manage_community(operation_input: EditCommunityInput) -> bool:
    """Return whether the caller is the owner or configured super-admin."""
    community = operation_input.get_local_community()
    if community is None:
        return False
    return can_manage_local_community(
        settings=operation_input.settings,
        discord_user_id=operation_input.discord_user_id,
        local_community=community,
    )


def _display_name_valid(operation_input: EditCommunityInput) -> bool:
    """Return whether display-name input satisfies shared metadata rules."""
    return operation_input.get_normalized_display_name() is not None


def _summary_valid(operation_input: EditCommunityInput) -> bool:
    """Return whether summary input satisfies shared metadata rules."""
    operation_input.get_normalized_summary()
    return operation_input._summary_error is None


def _status_valid(operation_input: EditCommunityInput) -> bool:
    """Return whether the modal submitted a supported lifecycle status."""
    return operation_input.get_normalized_status() is not None


def _inaccessible_message(operation_input: EditCommunityInput) -> str:
    """Build the shared inaccessible-community rejection text."""
    return f"Unknown or inaccessible local community: {operation_input.normalized_community_slug}"


def _display_name_error_message(operation_input: EditCommunityInput) -> str:
    """Return the stable display-name validation message."""
    return str(operation_input._display_name_error or "Community display name is required.")


def _summary_error_message(operation_input: EditCommunityInput) -> str:
    """Return the stable summary validation message."""
    return str(operation_input._summary_error or "Community summary must be 1000 characters or fewer.")


class EditCommunityOperation(Operation):
    """Declarative operation for editing local-community display metadata."""

    name = "edit_community"
    _REJECTION_REASONS = {
        "guild_context": "missing_guild_context",
        "community_accessible": "unknown_or_inaccessible_community",
        "can_manage_community": "cannot_manage_community",
        "display_name_valid": "invalid_display_name",
        "summary_valid": "invalid_summary",
        "status_valid": "invalid_status",
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
            name="display_name_valid",
            message=_display_name_error_message,
            predicate=_display_name_valid,
        ),
        Precondition(
            name="summary_valid",
            message=_summary_error_message,
            predicate=_summary_valid,
        ),
        Precondition(
            name="status_valid",
            message="Community status must be active or disabled.",
            predicate=_status_valid,
        ),
    )

    def reject(
        self,
        operation_input: EditCommunityInput,
        *,
        reason: str,
        message: str,
        **_: object,
    ) -> EditCommunityResult:
        """Return the first failed precondition as a command-visible result."""
        return EditCommunityResult(
            applied=False,
            message=message,
            reason=self._REJECTION_REASONS.get(reason, reason),
        )

    def body(self, operation_input: EditCommunityInput) -> EditCommunityResult:
        """Persist the validated metadata edit and report saved values."""
        community = operation_input.get_local_community()
        display_name = operation_input.get_normalized_display_name()
        summary = operation_input.get_normalized_summary()
        status = operation_input.get_normalized_status()
        if community is None or display_name is None or status is None:
            # Preconditions make this unreachable for normal command execution.
            # Keep a stable failure for future direct callers and test harnesses.
            return EditCommunityResult(
                applied=False,
                message="Unable to update community because the command state is invalid.",
                reason="invalid_operation_state",
            )

        updated = operation_input.database.local_communities.update_local_community_settings(
            local_community_id=community.id,
            display_name=display_name,
            summary=summary,
            status=status,
        )
        if updated is None:
            return EditCommunityResult(
                applied=False,
                message=f"Unknown or inaccessible local community: {operation_input.normalized_community_slug}",
                reason="unknown_or_inaccessible_community",
            )

        summary_label = updated.summary if updated.summary else "not specified"
        lifecycle_note = (
            "New posts, comments, follows, and subscriptions are now blocked."
            if updated.status == "disabled"
            else "New posts, comments, follows, and subscriptions are now allowed."
        )
        return EditCommunityResult(
            applied=True,
            message=(
                f"Updated community {updated.slug}.\n"
                f"Display name: {updated.display_name}\n"
                f"Summary: {summary_label}\n"
                f"Status: {updated.status}\n"
                f"{lifecycle_note}"
            ),
            reason="updated",
        )


def edit_community_operation(operation_input: EditCommunityInput) -> EditCommunityResult:
    """Execute `/edit-community` through ordered `discordops` preconditions."""
    return EditCommunityOperation().execute(operation_input)
