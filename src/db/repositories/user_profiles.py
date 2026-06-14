"""Persistence operations for local user profiles and immutable media metadata."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from ...models import ProfileMediaAsset, User, UserProfileField
from ...profile_media import PublishedMediaObject
from .base import BaseRepository


@dataclass(frozen=True, slots=True)
class UserProfileView:
    """Return normalized profile state without changing ActivityPub rendering."""

    user_id: int
    display_name: str
    stored_display_name: str | None
    summary: str | None
    fields: tuple[tuple[str, str], ...]
    avatar: ProfileMediaAsset | None
    header: ProfileMediaAsset | None


class UserProfileRepository(BaseRepository):
    """Own relational profile fields and media-slot references."""

    def get(self, user_id: int) -> UserProfileView | None:
        """Load one effective profile view in stable metadata order."""
        with self.session() as session:
            user = session.get(User, user_id)
            if user is None:
                return None
            fields = tuple(
                (row.name, row.value)
                for row in session.scalars(
                    select(UserProfileField)
                    .where(UserProfileField.user_id == user_id)
                    .order_by(UserProfileField.position)
                )
            )
            return UserProfileView(
                user_id=user.id,
                display_name=user.display_name or user.activitypub_username,
                stored_display_name=user.display_name,
                summary=user.summary,
                fields=fields,
                avatar=session.get(ProfileMediaAsset, user.avatar_media_id) if user.avatar_media_id else None,
                header=session.get(ProfileMediaAsset, user.header_media_id) if user.header_media_id else None,
            )

    @staticmethod
    def update_text(session: Session, user_id: int, display_name: str | None, summary: str | None, fields: list[tuple[str, str]]) -> None:
        """Replace all text profile state inside the caller-owned transaction."""
        user = session.get(User, user_id)
        if user is None:
            raise LookupError("registered user not found")
        user.display_name = display_name
        user.summary = summary
        session.execute(delete(UserProfileField).where(UserProfileField.user_id == user_id))
        for position, (name, value) in enumerate(fields):
            session.add(UserProfileField(user_id=user_id, position=position, name=name, value=value))

    @staticmethod
    def switch_slot(session: Session, user_id: int, slot: str, asset_id: int | None) -> None:
        """Switch exactly one typed media slot inside the caller-owned transaction."""
        user = session.get(User, user_id)
        if user is None:
            raise LookupError("registered user not found")
        if slot not in {"avatar", "header"}:
            raise ValueError("invalid profile media slot")
        setattr(user, f"{slot}_media_id", asset_id)
        session.flush()


class ProfileMediaRepository(BaseRepository):
    """Own immutable media rows and relational liveness queries."""

    @staticmethod
    def insert(session: Session, published: PublishedMediaObject) -> ProfileMediaAsset:
        """Insert metadata for bytes already durably published by the store."""
        asset = ProfileMediaAsset(**asdict(published))
        session.add(asset)
        session.flush()
        return asset

    def get_by_public_id(self, public_id: str) -> ProfileMediaAsset | None:
        """Load one publicly addressable asset by opaque identifier."""
        with self.session() as session:
            return session.scalar(select(ProfileMediaAsset).where(ProfileMediaAsset.public_id == public_id))

    @staticmethod
    def is_referenced(session: Session, asset_id: int) -> bool:
        """Check both user slots inside the cleanup write transaction."""
        return session.scalar(
            select(User.id).where(or_(User.avatar_media_id == asset_id, User.header_media_id == asset_id)).limit(1)
        ) is not None
