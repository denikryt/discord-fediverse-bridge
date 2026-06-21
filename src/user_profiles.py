"""Application service for atomic profile mutations and outbound update intent."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db.database import Database
from .db.repositories.user_profiles import UserProfileView
from .models import ProfileMediaAsset, User, UserProfileField
from .profile_media import FileLock, ProfileMediaStore
from .activitypub_identity_urls import build_user_profile_update_activity_id

if TYPE_CHECKING:
    from .user_actor_delivery import ProfileFanoutSummary, UserActorDeliveryService
    from .user_actor_delivery_worker import UserActorDeliveryWorker


class UserProfileValidationError(ValueError):
    """Report bounded user-profile validation failures."""


@dataclass(frozen=True, slots=True)
class ProfileMutationResult:
    """Return committed profile state and its durable federation source.

    Profile attributes are exposed as compatibility properties so existing
    callers can migrate without a temporary dual mutation API.
    """

    profile: UserProfileView
    profile_version: int
    source_id: int
    activity_id: str
    audience_count: int

    @property
    def user_id(self) -> int:
        """Expose the committed profile owner."""
        return self.profile.user_id

    @property
    def display_name(self) -> str:
        """Expose the effective display name."""
        return self.profile.display_name

    @property
    def stored_display_name(self) -> str | None:
        """Expose the nullable stored display name."""
        return self.profile.stored_display_name

    @property
    def summary(self) -> str | None:
        """Expose the committed summary."""
        return self.profile.summary

    @property
    def fields(self) -> tuple[tuple[str, str], ...]:
        """Expose committed ordered metadata fields."""
        return self.profile.fields

    @property
    def avatar(self) -> ProfileMediaAsset | None:
        """Expose the committed avatar asset."""
        return self.profile.avatar

    @property
    def header(self) -> ProfileMediaAsset | None:
        """Expose the committed header asset."""
        return self.profile.header


class UserProfileService:
    """Coordinate profile storage and atomic profile-update source creation."""

    SNAPSHOT_VERSION = 1

    def __init__(
        self,
        database: Database,
        store: ProfileMediaStore,
        public_origin: str = "https://bridge.invalid",
        *,
        delivery_service: UserActorDeliveryService | None = None,
        delivery_worker: UserActorDeliveryWorker | None = None,
    ) -> None:
        """Bind profile persistence and optional Stage 4 delivery owners."""
        self.database = database
        self.store = store
        self.public_origin = public_origin.rstrip("/")
        self.delivery_service = delivery_service
        self.delivery_worker = delivery_worker

    async def fanout(
        self, result: ProfileMutationResult
    ) -> ProfileFanoutSummary | None:
        """Wake recovery and make one optional inline attempt for command feedback."""
        if self.delivery_worker is not None:
            self.delivery_worker.wake()
        if self.delivery_service is None:
            return None
        return await self.delivery_service.process_source(result.source_id)

    def get_profile(self, user_id: int) -> UserProfileView | None:
        """Return one effective stored profile without rendering ActivityPub."""
        return self.database.user_profiles.get(user_id)

    def update_profile(
        self,
        user_id: int,
        display_name: str | None,
        summary: str | None,
        fields: list[tuple[str, str]],
    ) -> ProfileMutationResult:
        """Atomically replace all text fields and schedule one immutable update."""
        normalized_display = self._optional_text(display_name, 30, "display name")
        normalized_summary = self._optional_text(summary, 500, "summary")
        normalized_fields = self._normalize_fields(fields)
        return self._mutate(
            user_id,
            lambda session: self.database.user_profiles.update_text(
                session,
                user_id,
                normalized_display,
                normalized_summary,
                normalized_fields,
            ),
        )

    def update_identity_text(
        self, user_id: int, display_name: str | None, summary: str | None
    ) -> ProfileMutationResult:
        """Atomically replace display name and summary and schedule fanout."""
        normalized_display = self._optional_text(display_name, 30, "display name")
        normalized_summary = self._optional_text(summary, 500, "summary")
        return self._mutate(
            user_id,
            lambda session: self.database.user_profiles.update_scalar_text(
                session, user_id, normalized_display, normalized_summary
            ),
        )

    def replace_metadata(
        self, user_id: int, fields: list[tuple[str, str]]
    ) -> ProfileMutationResult:
        """Atomically replace ordered metadata and schedule fanout."""
        normalized_fields = self._normalize_fields(fields)
        return self._mutate(
            user_id,
            lambda session: self.database.user_profiles.replace_fields(
                session, user_id, normalized_fields
            ),
        )

    def replace_media(
        self, user_id: int, slot: str, source_stream: BinaryIO
    ) -> ProfileMutationResult:
        """Publish bytes, atomically switch the slot, and retain source media."""
        if slot not in {"avatar", "header"}:
            raise UserProfileValidationError("invalid profile media slot")
        published = self.store.stage_and_publish(slot, source_stream)

        def mutation(session: Session) -> None:
            asset = self.database.profile_media.insert(session, published)
            self.database.user_profiles.switch_slot(session, user_id, slot, asset.id)

        return self._mutate(user_id, mutation)

    def clear_media(self, user_id: int, slot: str) -> ProfileMutationResult:
        """Atomically clear one slot and schedule fanout."""
        if slot not in {"avatar", "header"}:
            raise UserProfileValidationError("invalid profile media slot")
        return self._mutate(
            user_id,
            lambda session: self.database.user_profiles.switch_slot(
                session, user_id, slot, None
            ),
        )

    def _mutate(
        self, user_id: int, mutation: Callable[[Session], None]
    ) -> ProfileMutationResult:
        """Own the profile, version, source, audience, and media transaction."""
        with self.database.immediate_session() as session:
            mutation(session)
            user = session.get(User, user_id)
            if user is None:
                raise LookupError("registered user not found")
            user.profile_version += 1
            session.flush()
            fields = list(
                session.scalars(
                    select(UserProfileField)
                    .where(UserProfileField.user_id == user_id)
                    .order_by(UserProfileField.position)
                )
            )
            avatar = (
                session.get(ProfileMediaAsset, user.avatar_media_id)
                if user.avatar_media_id
                else None
            )
            header = (
                session.get(ProfileMediaAsset, user.header_media_id)
                if user.header_media_id
                else None
            )
            snapshot = self._snapshot(user, fields, avatar, header)
            # Profile Update IDs are externally visible delivery source ids and
            # must follow the typed User Actor grammar after the Stage 4 cutover.
            activity_id = build_user_profile_update_activity_id(
                origin=self.public_origin,
                username=str(user.activitypub_username),
                profile_version_or_activity_id=str(user.profile_version),
            )
            followers = self.database.user_actor_followers.list_accepted_for_snapshot(
                session, user_id=user_id
            )
            media_refs: list[tuple[str, int]] = []
            if avatar is not None:
                media_refs.append(("avatar", avatar.id))
            if header is not None:
                media_refs.append(("header", header.id))
            source = self.database.user_actor_deliveries.insert_source_graph(
                session,
                user_id=user.id,
                logical_version=user.profile_version,
                activity_id=activity_id,
                profile_snapshot_json=json.dumps(
                    snapshot, separators=(",", ":"), sort_keys=True
                ),
                followers=followers,
                media_refs=media_refs,
            )
            audience_count = len(
                {
                    follower.remote_shared_inbox_url or follower.remote_inbox_url
                    for follower in followers
                }
            )
            profile = UserProfileView(
                user_id=user.id,
                display_name=user.display_name or user.activitypub_username,
                stored_display_name=user.display_name,
                summary=user.summary,
                fields=tuple((field.name, field.value) for field in fields),
                avatar=avatar,
                header=header,
            )
            return ProfileMutationResult(
                profile,
                user.profile_version,
                source.id,
                activity_id,
                audience_count,
            )

    def _snapshot(
        self,
        user: User,
        fields: list[UserProfileField],
        avatar: ProfileMediaAsset | None,
        header: ProfileMediaAsset | None,
    ) -> dict:
        """Build versioned non-secret canonical render input from committed rows."""

        def media(asset: ProfileMediaAsset | None) -> dict | None:
            if asset is None:
                return None
            return {
                "url": f"{self.public_origin}/media/profile/{asset.public_id}",
                "contentType": asset.content_type,
                "width": asset.width,
                "height": asset.height,
            }

        return {
            "schemaVersion": self.SNAPSHOT_VERSION,
            "actorId": user.actor_url,
            "username": user.activitypub_username,
            "displayName": user.display_name or user.activitypub_username,
            "summary": user.summary,
            "inboxId": user.inbox_url,
            "outboxId": user.outbox_url,
            "followersId": user.followers_url,
            "publicKeyId": f"{user.actor_url}#main-key",
            "publicKeyPem": user.public_key_pem,
            "fields": [{"name": field.name, "value": field.value} for field in fields],
            "avatar": media(avatar),
            "header": media(header),
        }

    def cleanup_orphans(
        self,
        *,
        grace_seconds: int,
        deployment_lock_path: Path | None = None,
        media_lock_path: Path,
        lock_timeout_seconds: float = 30,
        runtime_holds_deployment_lease: bool = False,
    ) -> int:
        """Delete old assets only when no current slot or executable source owns them."""
        deployment_lock = None
        if not runtime_holds_deployment_lease:
            if deployment_lock_path is None:
                raise ValueError("standalone cleanup requires a deployment lock path")
            deployment_lock = FileLock(deployment_lock_path).acquire(
                exclusive=False, timeout_seconds=lock_timeout_seconds
            )
        try:
            with FileLock(media_lock_path).acquire(
                exclusive=True, timeout_seconds=lock_timeout_seconds
            ):
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=grace_seconds)
                deleted = 0
                with self.database.immediate_session() as session:
                    # Release terminal source ownership before evaluating FK-safe
                    # orphan deletion in the same reserved cleanup transaction.
                    self.database.profile_media.release_releasable_source_media(session)
                    candidates = list(
                        session.scalars(
                            select(ProfileMediaAsset)
                            .where(ProfileMediaAsset.created_at < cutoff)
                            .order_by(ProfileMediaAsset.id)
                        )
                    )
                    for asset in candidates:
                        if self.database.profile_media.is_referenced(session, asset.id):
                            continue
                        try:
                            self.store.unlink(asset.storage_key)
                        except FileNotFoundError:
                            pass
                        session.delete(asset)
                        deleted += 1
                return deleted
        finally:
            if deployment_lock is not None:
                deployment_lock.close()

    def _normalize_fields(self, fields: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Validate the four-row plain-text metadata contract."""
        if len(fields) > 4:
            raise UserProfileValidationError("at most four profile fields are allowed")
        return [
            (
                self._required_text(name, 255, "field name"),
                self._required_text(value, 255, "field value"),
            )
            for name, value in fields
        ]

    @staticmethod
    def _normalize(value: str) -> str:
        """Apply persisted NFC and Unicode-whitespace normalization."""
        return unicodedata.normalize("NFC", value).strip()

    @classmethod
    def _optional_text(cls, value: str | None, limit: int, label: str) -> str | None:
        """Normalize optional text and convert empty content to NULL."""
        if value is None:
            return None
        normalized = cls._normalize(value)
        if len(normalized) > limit:
            raise UserProfileValidationError(f"{label} exceeds {limit} characters")
        return normalized or None

    @classmethod
    def _required_text(cls, value: str, limit: int, label: str) -> str:
        """Normalize required metadata text and reject blanks or overflow."""
        normalized = cls._normalize(value)
        if not normalized or len(normalized) > limit:
            raise UserProfileValidationError(f"invalid {label}")
        return normalized
