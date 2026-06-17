"""Application service for personal-feed channel assignment and lookup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .channel_ownership import resolve_channel_owner
from .db import Database
from .models import PersonalFeedBinding, User


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
        with self.database.session() as session:
            row = session.execute(
                select(PersonalFeedBinding, User)
                .join(User)
                .where(User.discord_user_id == str(discord_user_id))
            ).first()
            return None if row is None else self._view(row[0], row[1])

    def resolve_root_binding(
        self, guild_id: int, channel_id: int
    ) -> PersonalFeedBindingView | None:
        """Resolve exact durable root ownership by positive guild and channel IDs."""
        if guild_id <= 0 or channel_id <= 0:
            return None
        with self.database.session() as session:
            row = session.execute(
                select(PersonalFeedBinding, User)
                .join(User)
                .where(
                    PersonalFeedBinding.discord_guild_id == guild_id,
                    PersonalFeedBinding.discord_channel_id == channel_id,
                )
            ).first()
            return None if row is None else self._view(row[0], row[1])

    def is_active_personal_feed_channel(self, user_id: int, channel_id: int) -> bool:
        """Compare a historical publication channel with the user's current binding."""
        binding = self.database.personal_feed_bindings.get_by_user_id(user_id)
        return binding is not None and binding.discord_channel_id == channel_id

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
                    current.discord_guild_id = guild_id
                    current.discord_channel_id = channel_id
                session.flush()
                return PersonalFeedMutationResult(status, self._view(current, user))
        except IntegrityError:
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
            if current is None:
                return PersonalFeedMutationResult("not_bound")
            view = self._view(current, user)
            session.delete(current)
            return PersonalFeedMutationResult("unbound", view)
