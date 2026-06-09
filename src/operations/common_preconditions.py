"""Shared DiscordOps inputs, preconditions, and command-access policies.

This module owns reusable access declarations that are consumed by both
Discord command ingress and domain operations. It intentionally depends only
on primitive project types and DiscordOps, never on the Discord SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from discordops import PolicyDefinition, Precondition

from ..config import Settings
from ..bridge_policy import BridgePolicyService, BridgePolicySnapshot
from ..user_bans import BanDecision, UserBanService, render_ban_message
from ..db import Database
from ..local_community_lifecycle import disabled_moderation_message, is_local_community_disabled
from ..local_community_permissions import (
    can_access_local_community_from_guild,
    can_manage_local_community,
    is_super_admin,
)
from ..models import LocalCommunity

GUILD_ONLY_MESSAGE = "This command can only be used inside an allowed Discord server."
GUILD_NOT_ALLOWED_MESSAGE = "This Discord server is not allowed to use this bridge bot."
REGISTRATION_REQUIRED_MESSAGE = "You must register with the bridge before using this command. Use `/register` first."


class RegisteredDiscordUserInput(Protocol):
    """Structural contract for inputs that can resolve shared command access."""

    def get_policy_snapshot(self) -> BridgePolicySnapshot:
        """Return one memoized effective policy snapshot."""
        ...

    def get_global_ban_decision(self) -> BanDecision:
        """Return one memoized global-ban decision for the caller."""
        ...

    def get_bridge_user(self) -> object | None:
        """Return the registered bridge user or ``None`` when absent."""
        ...


class OptionalCommunityScopeInput(Protocol):
    """Structural contract for operations with global or local-community scope.

    Implementations own repository access and memoization through
    ``get_local_community``. Shared predicates must never bypass that method.
    """

    settings: Settings
    discord_user_id: str
    def get_policy_snapshot(self) -> BridgePolicySnapshot:
        """Return one memoized effective policy snapshot."""
        ...
    discord_guild_id: int | None

    @property
    def is_global(self) -> bool:
        """Return whether the operation targets bridge-wide scope."""
        ...

    @property
    def normalized_community_slug(self) -> str | None:
        """Return the selected local-community slug or ``None`` globally."""
        ...

    def get_local_community(self) -> LocalCommunity | None:
        """Return the memoized selected local community when scoped."""
        ...


class RequiredLocalCommunityInput(Protocol):
    """Structural contract for operations that always target one community."""

    settings: Settings
    discord_user_id: str
    def get_policy_snapshot(self) -> BridgePolicySnapshot:
        """Return one memoized effective policy snapshot."""
        ...
    discord_guild_id: int | None

    @property
    def normalized_community_slug(self) -> str:
        """Return the selected local-community slug."""
        ...

    def get_local_community(self) -> LocalCommunity | None:
        """Return the memoized selected local community."""
        ...


def is_global_scope_authorized(value: OptionalCommunityScopeInput) -> bool:
    """Allow global scope only to a configured bridge super-admin."""
    return not value.is_global or is_super_admin(
        policy_snapshot=value.get_policy_snapshot(),
        discord_user_id=value.discord_user_id,
    )


def has_required_scoped_guild_context(value: OptionalCommunityScopeInput) -> bool:
    """Require guild context for community scope while allowing global scope."""
    return value.is_global or value.discord_guild_id is not None


def is_scoped_local_community_accessible(value: OptionalCommunityScopeInput) -> bool:
    """Allow global scope or an accessible selected local community."""
    if value.is_global:
        # Global operations must not touch community repositories.
        return True
    community = value.get_local_community()
    return community is not None and can_access_local_community_from_guild(
        policy_snapshot=value.get_policy_snapshot(),
        discord_user_id=value.discord_user_id,
        discord_guild_id=value.discord_guild_id,
        local_community=community,
        include_disabled=True,
    )


def can_manage_scoped_local_community(value: OptionalCommunityScopeInput) -> bool:
    """Allow global scope or management of the selected local community."""
    if value.is_global:
        return True
    community = value.get_local_community()
    return community is not None and can_manage_local_community(
        policy_snapshot=value.get_policy_snapshot(),
        discord_user_id=value.discord_user_id,
        local_community=community,
    )


def is_scoped_local_community_moderation_enabled(
    value: OptionalCommunityScopeInput,
) -> bool:
    """Allow global scope or an enabled selected local community."""
    if value.is_global:
        return True
    community = value.get_local_community()
    return community is not None and not is_local_community_disabled(community)


def has_required_local_community_guild_context(
    value: RequiredLocalCommunityInput,
) -> bool:
    """Require a Discord guild context for a local-community operation."""
    return value.discord_guild_id is not None


def is_required_local_community_accessible(
    value: RequiredLocalCommunityInput,
) -> bool:
    """Require the selected local community to exist and be accessible."""
    community = value.get_local_community()
    return community is not None and can_access_local_community_from_guild(
        policy_snapshot=value.get_policy_snapshot(),
        discord_user_id=value.discord_user_id,
        discord_guild_id=value.discord_guild_id,
        local_community=community,
        include_disabled=True,
    )


def can_manage_required_local_community(
    value: RequiredLocalCommunityInput,
) -> bool:
    """Require management permission for the selected local community."""
    community = value.get_local_community()
    return community is not None and can_manage_local_community(
        policy_snapshot=value.get_policy_snapshot(),
        discord_user_id=value.discord_user_id,
        local_community=community,
    )


def inaccessible_community_message(
    value: OptionalCommunityScopeInput | RequiredLocalCommunityInput,
) -> str:
    """Build the stable inaccessible-community rejection text."""
    return (
        "Unknown or inaccessible local community: "
        f"{value.normalized_community_slug}"
    )


def disabled_scoped_community_message(value: OptionalCommunityScopeInput) -> str:
    """Build the stable disabled-community moderation rejection text."""
    return disabled_moderation_message(value.normalized_community_slug or "")


def global_scope_authorized_precondition(*, message: str) -> Precondition:
    """Build the shared global-scope check with operation-specific wording."""
    return Precondition(
        name="global_scope_requires_super_admin",
        message=message,
        predicate=is_global_scope_authorized,
    )


SCOPED_GUILD_CONTEXT_REQUIRED = Precondition(
    name="missing_guild_context",
    message="This command can only be used inside a guild.",
    predicate=has_required_scoped_guild_context,
)

SCOPED_LOCAL_COMMUNITY_ACCESSIBLE = Precondition(
    name="unknown_or_inaccessible_community",
    message=inaccessible_community_message,
    predicate=is_scoped_local_community_accessible,
)

SCOPED_LOCAL_COMMUNITY_MANAGEMENT_ALLOWED = Precondition(
    name="cannot_manage_community",
    message="You are not allowed to manage this local community.",
    predicate=can_manage_scoped_local_community,
)

SCOPED_LOCAL_COMMUNITY_MODERATION_ENABLED = Precondition(
    name="community_disabled",
    message=disabled_scoped_community_message,
    predicate=is_scoped_local_community_moderation_enabled,
)

LOCAL_COMMUNITY_GUILD_CONTEXT_REQUIRED = Precondition(
    name="missing_guild_context",
    message="This command can only be used inside a guild.",
    predicate=has_required_local_community_guild_context,
)

LOCAL_COMMUNITY_ACCESSIBLE = Precondition(
    name="unknown_or_inaccessible_community",
    message=inaccessible_community_message,
    predicate=is_required_local_community_accessible,
)

LOCAL_COMMUNITY_MANAGEMENT_ALLOWED = Precondition(
    name="cannot_manage_community",
    message="You are not allowed to manage this local community.",
    predicate=can_manage_required_local_community,
)


@dataclass(slots=True)
class CommandAccessInput:
    """Carry primitive command context and memoized registration state.

    The input contains no Discord SDK object, so the same policies can be used
    by slash commands, modal submissions, autocomplete, and focused tests.
    Registration lookup is memoized per interaction input to avoid duplicate
    repository reads when one policy or caller resolves the user repeatedly.
    """

    settings: Settings
    database: Database | Any | None
    discord_guild_id: int | None
    discord_user_id: str
    policy_service: BridgePolicyService
    member_can_manage_guild: bool = False
    ban_service: UserBanService | None = None
    _policy_snapshot: BridgePolicySnapshot | None = field(default=None, init=False, repr=False)
    _policy_snapshot_loaded: bool = field(default=False, init=False, repr=False)
    _global_ban_decision: BanDecision | None = field(default=None, init=False, repr=False)
    _bridge_user: object | None = field(default=None, init=False, repr=False)
    _bridge_user_loaded: bool = field(default=False, init=False, repr=False)

    def get_policy_snapshot(self) -> BridgePolicySnapshot:
        """Resolve one memoized effective policy snapshot for all access checks."""
        if not self._policy_snapshot_loaded:
            self._policy_snapshot = self.policy_service.snapshot()
            self._policy_snapshot_loaded = True
        assert self._policy_snapshot is not None
        return self._policy_snapshot

    def get_global_ban_decision(self) -> BanDecision:
        """Resolve one memoized global-ban decision for this command input."""
        if self._global_ban_decision is None:
            if self.ban_service is None:
                if self.database is None:
                    raise RuntimeError("Global-ban access requires a database.")
                self.ban_service = UserBanService(
                    database=self.database,
                    settings=self.settings,
                )
            self._global_ban_decision = self.ban_service.check_global_discord_user(
                self.discord_user_id
            )
        return self._global_ban_decision

    def get_bridge_user(self) -> object | None:
        """Resolve and memoize the registered bridge user for this command input."""
        if not self._bridge_user_loaded:
            if self.database is None:
                raise RuntimeError("Registered command access requires a database.")
            self._bridge_user = self.database.users.get_user_by_discord_user_id(self.discord_user_id)
            self._bridge_user_loaded = True
        return self._bridge_user


def _has_guild_context(value: CommandAccessInput) -> bool:
    """Return whether the interaction originated from a Discord guild."""
    return value.discord_guild_id is not None


def _is_global_user_not_banned(value: RegisteredDiscordUserInput) -> bool:
    """Admit only callers without an active global bridge ban."""
    return not value.get_global_ban_decision().banned


def _global_ban_message(value: RegisteredDiscordUserInput) -> str:
    """Render the existing scope-specific global-ban rejection text."""
    return render_ban_message(value.get_global_ban_decision())


def _is_command_guild_allowed(value: CommandAccessInput) -> bool:
    """Apply effective dynamic/bootstrap guild policy for this interaction."""
    return value.get_policy_snapshot().is_discord_guild_allowed(value.discord_guild_id)


def _can_member_manage_guild(value: CommandAccessInput) -> bool:
    """Return whether the invoking guild member has Discord Manage Guild."""
    return value.member_can_manage_guild


def _is_discord_user_registered(value: RegisteredDiscordUserInput) -> bool:
    """Return whether the input resolves an existing registered bridge user."""
    return value.get_bridge_user() is not None


GUILD_CONTEXT_REQUIRED = Precondition(
    name="no_guild",
    message=GUILD_ONLY_MESSAGE,
    predicate=_has_guild_context,
)

GLOBAL_USER_NOT_BANNED = Precondition(
    name="globally_banned_user",
    message=_global_ban_message,
    predicate=_is_global_user_not_banned,
)

GUILD_POLICY_ALLOWS_COMMAND = Precondition(
    name="guild_not_allowed",
    message=GUILD_NOT_ALLOWED_MESSAGE,
    predicate=_is_command_guild_allowed,
)

MANAGE_GUILD_REQUIRED = Precondition(
    name="missing_manage_guild",
    message="You need the Manage Server permission to manage this server invite.",
    predicate=_can_member_manage_guild,
)

DISCORD_USER_REGISTERED = Precondition(
    name="discord_user_not_registered",
    message=REGISTRATION_REQUIRED_MESSAGE,
    predicate=_is_discord_user_registered,
)

# Named compositions keep command handlers declarative while preserving one
# source of truth for atomic preconditions and their short-circuit order.
GUILD_COMMAND_ACCESS = PolicyDefinition(
    name="guild_command_access",
    preconditions=(GLOBAL_USER_NOT_BANNED, GUILD_CONTEXT_REQUIRED, GUILD_POLICY_ALLOWS_COMMAND),
)

REGISTERED_GUILD_COMMAND_ACCESS = PolicyDefinition(
    name="registered_guild_command_access",
    preconditions=(GLOBAL_USER_NOT_BANNED, GUILD_CONTEXT_REQUIRED, GUILD_POLICY_ALLOWS_COMMAND, DISCORD_USER_REGISTERED),
)

MANAGE_GUILD_COMMAND_ACCESS = PolicyDefinition(
    name="manage_guild_command_access",
    preconditions=(GLOBAL_USER_NOT_BANNED, GUILD_CONTEXT_REQUIRED, GUILD_POLICY_ALLOWS_COMMAND, MANAGE_GUILD_REQUIRED),
)
