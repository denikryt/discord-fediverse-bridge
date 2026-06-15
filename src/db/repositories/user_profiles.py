"""Persistence operations for local user profiles and immutable media metadata."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from ...models import (
    ProfileMediaAsset,
    User,
    UserActorOutboundDelivery,
    UserActorOutboundSource,
    UserActorOutboundSourceMedia,
    UserProfileField,
)
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
                avatar=(
                    session.get(ProfileMediaAsset, user.avatar_media_id)
                    if user.avatar_media_id
                    else None
                ),
                header=(
                    session.get(ProfileMediaAsset, user.header_media_id)
                    if user.header_media_id
                    else None
                ),
            )

    @staticmethod
    def update_scalar_text(
        session: Session, user_id: int, display_name: str | None, summary: str | None
    ) -> None:
        """Replace only scalar profile text in the caller-owned transaction."""
        user = session.get(User, user_id)
        if user is None:
            raise LookupError("registered user not found")
        user.display_name = display_name
        user.summary = summary

    @staticmethod
    def replace_fields(
        session: Session, user_id: int, fields: list[tuple[str, str]]
    ) -> None:
        """Replace ordered metadata fields without changing scalar profile text."""
        if session.get(User, user_id) is None:
            raise LookupError("registered user not found")
        session.execute(
            delete(UserProfileField).where(UserProfileField.user_id == user_id)
        )
        for position, (name, value) in enumerate(fields):
            session.add(
                UserProfileField(
                    user_id=user_id, position=position, name=name, value=value
                )
            )

    @classmethod
    def update_text(
        cls,
        session: Session,
        user_id: int,
        display_name: str | None,
        summary: str | None,
        fields: list[tuple[str, str]],
    ) -> None:
        """Preserve the Stage 1 atomic combined-update compatibility contract."""
        cls.update_scalar_text(session, user_id, display_name, summary)
        cls.replace_fields(session, user_id, fields)

    @staticmethod
    def switch_slot(
        session: Session, user_id: int, slot: str, asset_id: int | None
    ) -> None:
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
            return session.scalar(
                select(ProfileMediaAsset).where(
                    ProfileMediaAsset.public_id == public_id
                )
            )

    @staticmethod
    def release_releasable_source_media(session: Session) -> int:
        """Delete media ownership rows whose source can no longer execute."""
        references = list(session.scalars(select(UserActorOutboundSourceMedia)))
        released = 0
        for reference in references:
            source = session.get(UserActorOutboundSource, reference.source_id)
            if source is None or source.state != "ready":
                continue
            unfinished = session.scalar(
                select(UserActorOutboundDelivery.id)
                .where(
                    UserActorOutboundDelivery.source_id == source.id,
                    UserActorOutboundDelivery.status.in_(
                        ("pending", "sending", "failed")
                    ),
                )
                .limit(1)
            )
            if unfinished is None:
                session.delete(reference)
                released += 1
        session.flush()
        return released

    @staticmethod
    def is_referenced(session: Session, asset_id: int) -> bool:
        """Check both user slots inside the cleanup write transaction."""
        current_slot = (
            session.scalar(
                select(User.id)
                .where(
                    or_(
                        User.avatar_media_id == asset_id,
                        User.header_media_id == asset_id,
                    )
                )
                .limit(1)
            )
            is not None
        )
        if current_slot:
            return True
        # A historical source owns media until rendering is complete and every
        # endpoint is terminal; this preserves immutable retry payloads.
        live_source = session.scalar(
            select(UserActorOutboundSourceMedia.id)
            .join(
                UserActorOutboundSource,
                UserActorOutboundSource.id
                == UserActorOutboundSourceMedia.source_id,
            )
            .where(
                UserActorOutboundSourceMedia.media_asset_id == asset_id,
                or_(
                    UserActorOutboundSource.state != "ready",
                    select(UserActorOutboundDelivery.id)
                    .where(
                        UserActorOutboundDelivery.source_id == UserActorOutboundSource.id,
                        UserActorOutboundDelivery.status.in_(
                        ("pending", "sending", "failed")
                    ),
                    )
                    .exists(),
                ),
            )
            .limit(1)
        )
        return live_source is not None
