"""Application service for personal-feed channel assignment and lookup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .channel_ownership import resolve_channel_owner
from .db import Database
from .models import PersonalFeedBinding, User, ChannelSurface, PlatformConnector
from .db.repositories.platform_surfaces import (
    PlatformSurfaceRepository,
    discord_claim_query,
)


@dataclass(frozen=True)
class PersonalFeedBindingView:
    """Expose non-secret current routing identity for commands and event admission."""

    id: int
    user_id: int
    discord_user_id: str
    discord_guild_id: int
    discord_channel_id: int


@dataclass(frozen=True)
class PersonalFeedMutationResult:
    """Describe one completed binding mutation without leaking persistence objects."""

    status: str
    binding: PersonalFeedBindingView | None = None
    conflict_kind: str | None = None


_EXPECTED_BINDING_UNSPECIFIED: Final = object()


class PersonalFeedService:
    """Own atomic bind/move/unbind semantics and durable active-binding queries."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _view(binding: PersonalFeedBinding, user: User) -> PersonalFeedBindingView:
        """Convert ORM state into a detached immutable routing view."""
        # MULTIPLATFORM_DISCORD_PROJECTION_TEMP: Discord personal-feed view requires a primary Discord surface claim and complete location.
        if (
            binding.discord_guild_id is None
            or binding.discord_channel_id is None
            or binding.discord_guild_id <= 0
            or binding.discord_channel_id <= 0
        ):
            raise ValueError(
                "Personal Feed view requires a complete positive Discord location"
            )
        return PersonalFeedBindingView(
            binding.id,
            binding.user_id,
            user.discord_user_id,
            binding.discord_guild_id,
            binding.discord_channel_id,
        )

    def get_by_discord_user_id(
        self, discord_user_id: str
    ) -> PersonalFeedBindingView | None:
        """Return one caller's current binding for account-settings display."""
        # Display and event admission share the repository's physical-claim checks.
        with self.database.session() as session:
            row = session.execute(
                discord_claim_query(PersonalFeedBinding, "personal_feed_binding_id")
                .join(User, User.id == PersonalFeedBinding.user_id)
                .add_columns(User)
                .where(User.discord_user_id == str(discord_user_id))
            ).first()
            return None if row is None else self._view(row[0], row[1])

    def resolve_root_binding(
        self, guild_id: int, channel_id: int
    ) -> PersonalFeedBindingView | None:
        """Resolve exact durable root ownership by positive guild and channel IDs."""
        if guild_id <= 0 or channel_id <= 0:
            return None
        # Display and event admission share the repository's physical-claim checks.
        with self.database.session() as session:
            row = session.execute(
                discord_claim_query(PersonalFeedBinding, "personal_feed_binding_id")
                .join(User, User.id == PersonalFeedBinding.user_id)
                .add_columns(User)
                .where(
                    PersonalFeedBinding.discord_guild_id == guild_id,
                    PersonalFeedBinding.discord_channel_id == channel_id,
                )
            ).first()
            return None if row is None else self._view(row[0], row[1])

    def is_active_personal_feed_channel(self, user_id: int, channel_id: int) -> bool:
        """Compare a historical publication channel with the user's current binding."""
        binding = self.database.personal_feed_bindings.get_by_channel_id(channel_id)
        return (
            binding is not None
            and binding.user_id == user_id
            and binding.discord_channel_id == channel_id
        )

    def bind_or_move(
        self,
        discord_user_id: str,
        guild_id: int,
        channel_id: int,
        *,
        expected_binding_id: int | None | object = _EXPECTED_BINDING_UNSPECIFIED,
    ) -> PersonalFeedMutationResult:
        """Atomically create or compare-and-set one user's current binding.

        Command callers pass the binding ID observed before worker submission.
        An explicit ``None`` means the caller observed no binding, so a racing
        first-bind cannot silently become a move after waiting for SQLite's
        writer reservation. Omitted expectation preserves internal callers that
        deliberately request unconditional move semantics.
        """
        if guild_id <= 0 or channel_id <= 0:
            return PersonalFeedMutationResult("invalid_target")
        try:
            with self.database.immediate_session() as session:
                user = session.scalar(
                    select(User).where(User.discord_user_id == str(discord_user_id))
                )
                if user is None:
                    return PersonalFeedMutationResult("not_registered")
                current = session.scalar(
                    select(PersonalFeedBinding).where(
                        PersonalFeedBinding.user_id == user.id
                    )
                )
                old_surface = None
                if current is not None:
                    old_surface = session.scalar(
                        select(ChannelSurface)
                        .join(PlatformConnector)
                        .where(
                            ChannelSurface.personal_feed_binding_id == current.id,
                            PlatformConnector.connector_key == "discord-primary",
                            PlatformConnector.platform == "discord",
                        )
                    )
                    if (
                        old_surface is None
                        or current.discord_guild_id is None
                        or current.discord_channel_id is None
                    ):
                        return PersonalFeedMutationResult("binding_changed")
                if expected_binding_id is not _EXPECTED_BINDING_UNSPECIFIED:
                    current_id = None if current is None else current.id
                    if current_id != expected_binding_id:
                        return PersonalFeedMutationResult("binding_changed")
                if (
                    current is not None
                    and current.discord_guild_id == guild_id
                    and current.discord_channel_id == channel_id
                ):
                    return PersonalFeedMutationResult(
                        "already_bound", self._view(current, user)
                    )
                guild_owner = session.scalar(
                    select(PersonalFeedBinding).where(
                        PersonalFeedBinding.discord_guild_id == guild_id
                    )
                )
                if guild_owner is not None and (
                    current is None or guild_owner.id != current.id
                ):
                    return PersonalFeedMutationResult(
                        "guild_conflict", conflict_kind="personal_feed"
                    )
                owner = resolve_channel_owner(session, channel_id)
                if owner is not None and (
                    current is None
                    or owner.kind != "personal_feed"
                    or owner.row_id != current.id
                ):
                    return PersonalFeedMutationResult(
                        "channel_conflict", conflict_kind=owner.kind
                    )
                status = "bound" if current is None else "moved"
                if current is None:
                    current = PersonalFeedBinding(
                        user_id=user.id,
                        discord_guild_id=guild_id,
                        discord_channel_id=channel_id,
                    )
                    session.add(current)
                else:
                    PlatformSurfaceRepository.release_claim(
                        session,
                        old_surface.id,
                        owner_column="personal_feed_binding_id",
                        expected_owner_id=current.id,
                    )
                    current.discord_guild_id = guild_id
                    current.discord_channel_id = channel_id
                session.flush()
                existing_surface = session.scalar(
                    select(ChannelSurface)
                    .join(PlatformConnector)
                    .where(
                        PlatformConnector.connector_key == "discord-primary",
                        ChannelSurface.external_channel_id == str(channel_id),
                    )
                )
                # Both text and announcement channels are valid existing feed
                # targets; do not overwrite more precise persisted metadata.
                target_kind = (
                    existing_surface.kind
                    if existing_surface is not None
                    and existing_surface.kind in {"text", "announcement"}
                    else "text"
                )
                new_surface = PlatformSurfaceRepository.discord_channel(
                    session, channel_id, guild_id, target_kind
                )
                PlatformSurfaceRepository.claim_personal_feed(
                    session, new_surface.id, current.id
                )
                return PersonalFeedMutationResult(status, self._view(current, user))
        except (IntegrityError, ValueError):
            return PersonalFeedMutationResult("channel_conflict")

    def unbind(self, discord_user_id: str) -> PersonalFeedMutationResult:
        """Delete only the caller's current binding and remain idempotent."""
        with self.database.immediate_session() as session:
            user = session.scalar(
                select(User).where(User.discord_user_id == str(discord_user_id))
            )
            if user is None:
                return PersonalFeedMutationResult("not_registered")
            current = session.scalar(
                select(PersonalFeedBinding).where(
                    PersonalFeedBinding.user_id == user.id
                )
            )
            surface = (
                session.scalar(
                    select(ChannelSurface)
                    .join(PlatformConnector)
                    .where(
                        ChannelSurface.personal_feed_binding_id == current.id,
                        PlatformConnector.connector_key == "discord-primary",
                        PlatformConnector.platform == "discord",
                    )
                )
                if current is not None
                else None
            )
            if current is None or surface is None:
                return PersonalFeedMutationResult("not_bound")
            view = self._view(current, user)
            PlatformSurfaceRepository.release_claim(
                session,
                surface.id,
                owner_column="personal_feed_binding_id",
                expected_owner_id=current.id,
            )
            session.delete(current)
            return PersonalFeedMutationResult("unbound", view)
