"""Identity resolution, policy decisions, and user-visible text for bridge bans."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from .config import Settings
from .fediverse_identity import InvalidRemoteActorHandle, normalize_remote_actor_handle


@dataclass(frozen=True, slots=True)
class ResolvedBanTarget:
    """Canonical target metadata stored by one moderation command."""

    actor_handle: str
    actor_url: str | None
    discord_user_id: str | None
    is_local: bool


@dataclass(frozen=True, slots=True)
class BanDecision:
    """Describe the effective active ban selected by policy precedence."""

    banned: bool
    scope: str | None = None
    reason: str | None = None
    community_handle: str | None = None
    ban: object | None = None

    @property
    def reason_text(self) -> str:
        """Return stable user-visible reason text."""
        return self.reason or "No reason provided."


class UnknownLocalBanTarget(ValueError):
    """Signal that a local-domain handle has no registered bridge user."""


def public_authority(settings: Settings) -> str:
    """Return the configured public host and optional port for canonical handles."""
    base_url = getattr(settings, "normalized_public_base_url", None) or str(getattr(settings, "public_base_url", "https://bridge.invalid"))
    parsed = urlparse(base_url)
    return parsed.netloc.lower()


def canonical_local_user_handle(*, username: str, settings: Settings) -> str:
    """Build one registered user's stable local Fediverse handle."""
    return f"{username}@{public_authority(settings)}"


def canonical_local_community_handle(*, slug: str, settings: Settings) -> str:
    """Build one local community's stable bridge handle."""
    return f"{slug}@{public_authority(settings)}"


def resolve_ban_target(*, database: object, settings: Settings, value: str) -> ResolvedBanTarget:
    """Resolve local handles from DB and normalize remote handles without network I/O."""
    handle = normalize_remote_actor_handle(value)
    username, domain = handle.rsplit("@", 1)
    if domain.casefold() != public_authority(settings).casefold():
        return ResolvedBanTarget(handle, None, None, False)
    user = database.users.get_user_by_activitypub_username(username)
    if user is None:
        raise UnknownLocalBanTarget("No registered local user exists for that handle.")
    return ResolvedBanTarget(
        canonical_local_user_handle(username=user.activitypub_username, settings=settings),
        user.actor_url,
        str(user.discord_user_id),
        True,
    )


def render_ban_message(decision: BanDecision) -> str:
    """Render scope-specific Discord rejection or notification text."""
    if decision.scope == "community":
        heading = f"You were banned from community {decision.community_handle}."
    else:
        heading = "You were banned from this bridge instance."
    return f"{heading}\nReason: {decision.reason_text}"


class UserBanService:
    """Resolve effective bans for Discord and ActivityPub actions."""

    def __init__(self, *, database: object, settings: Settings | None) -> None:
        """Initialise policy over persistence and canonical public identity config."""
        self.database = database
        self.settings = settings

    def check_global_discord_user(self, discord_user_id: str) -> BanDecision:
        """Return an active global ban attached to one immutable Discord id."""
        ban = self.database.community_actor_bans.get_active_global_ban_by_discord_user_id(
            discord_user_id=discord_user_id
        )
        return self._decision(ban)

    def check_discord_user(self, *, discord_user_id: str, local_community: object | None) -> BanDecision:
        """Apply global-first then community-scoped Discord moderation precedence."""
        global_decision = self.check_global_discord_user(discord_user_id)
        if global_decision.banned or local_community is None:
            return global_decision
        ban = self.database.community_actor_bans.get_active_community_ban_by_discord_user_id(
            local_community_id=int(local_community.id), discord_user_id=discord_user_id
        )
        return self._decision(ban, local_community=local_community)

    def check_activitypub_actor(
        self, *, local_community_id: int, actor_url: str, actor_handle: str | None
    ) -> BanDecision:
        """Apply URL-first/handle-fallback matching globally then per community."""
        ban = self.database.community_actor_bans.find_active_ban_for_actor(
            local_community_id=None, actor_url=actor_url, actor_handle=actor_handle
        )
        if ban is None:
            ban = self.database.community_actor_bans.find_active_ban_for_actor(
                local_community_id=local_community_id, actor_url=actor_url, actor_handle=actor_handle
            )
        return self._decision(ban)

    def _decision(self, ban: object | None, *, local_community: object | None = None) -> BanDecision:
        """Convert one persisted row into a transport-independent policy result."""
        if ban is None:
            return BanDecision(False)
        community_handle = None
        if getattr(ban, "scope", None) == "community" and local_community is not None and self.settings is not None:
            community_handle = canonical_local_community_handle(
                slug=str(local_community.slug), settings=self.settings
            )
        return BanDecision(
            True,
            scope=str(getattr(ban, "scope", "community")),
            reason=getattr(ban, "reason", None),
            community_handle=community_handle,
            ban=ban,
        )
