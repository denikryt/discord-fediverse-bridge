"""Application service for atomic local profile and profile-media mutations."""

from __future__ import annotations

import unicodedata
from typing import BinaryIO

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from .db.database import Database
from .db.repositories.user_profiles import UserProfileView
from .models import ProfileMediaAsset
from .profile_media import FileLock, ProfileMediaStore


class UserProfileValidationError(ValueError):
    """Report bounded user-profile validation failures."""


class UserProfileService:
    """Coordinate profile repositories and durable media storage transactions."""

    def __init__(self, database: Database, store: ProfileMediaStore) -> None:
        self.database = database
        self.store = store

    def get_profile(self, user_id: int) -> UserProfileView | None:
        """Return one effective stored profile without rendering ActivityPub."""
        return self.database.user_profiles.get(user_id)

    def update_profile(self, user_id: int, display_name: str | None, summary: str | None, fields: list[tuple[str, str]]) -> UserProfileView:
        """Normalize and atomically replace scalar and ordered metadata fields."""
        normalized_display = self._optional_text(display_name, 30, "display name")
        normalized_summary = self._optional_text(summary, 500, "summary")
        normalized_fields = self._normalize_fields(fields)
        with self.database.session() as session:
            self.database.user_profiles.update_text(session, user_id, normalized_display, normalized_summary, normalized_fields)
        profile = self.get_profile(user_id)
        if profile is None:
            raise LookupError("registered user not found")
        return profile


    def update_identity_text(
        self, user_id: int, display_name: str | None, summary: str | None
    ) -> UserProfileView:
        """Atomically replace display name and summary without touching metadata."""
        normalized_display = self._optional_text(display_name, 30, "display name")
        normalized_summary = self._optional_text(summary, 500, "summary")
        with self.database.session() as session:
            self.database.user_profiles.update_scalar_text(
                session, user_id, normalized_display, normalized_summary
            )
        return self._require_profile(user_id)

    def replace_metadata(
        self, user_id: int, fields: list[tuple[str, str]]
    ) -> UserProfileView:
        """Atomically replace ordered metadata without touching scalar profile text."""
        normalized_fields = self._normalize_fields(fields)
        with self.database.session() as session:
            self.database.user_profiles.replace_fields(session, user_id, normalized_fields)
        return self._require_profile(user_id)

    def replace_media(self, user_id: int, slot: str, source_stream: BinaryIO) -> UserProfileView:
        """Publish bytes first, then atomically insert metadata and switch one slot."""
        if slot not in {"avatar", "header"}:
            raise UserProfileValidationError("invalid profile media slot")
        published = self.store.stage_and_publish(slot, source_stream)
        with self.database.session() as session:
            asset = self.database.profile_media.insert(session, published)
            self.database.user_profiles.switch_slot(session, user_id, slot, asset.id)
        return self._require_profile(user_id)

    def clear_media(self, user_id: int, slot: str) -> UserProfileView:
        """Clear exactly one slot without synchronously unlinking prior bytes."""
        with self.database.session() as session:
            self.database.user_profiles.switch_slot(session, user_id, slot, None)
        return self._require_profile(user_id)

    def cleanup_orphans(
        self,
        *,
        grace_seconds: int,
        deployment_lock_path: Path | None = None,
        media_lock_path: Path,
        lock_timeout_seconds: float = 30,
        runtime_holds_deployment_lease: bool = False,
    ) -> int:
        """Delete old unreferenced objects under cross-process and SQLite exclusion."""
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
                # BEGIN IMMEDIATE prevents a slot switch from committing between
                # the final liveness check and physical unlink/row deletion.
                with self.database.immediate_session() as session:
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
                            # A prior cleanup may have unlinked bytes before its DB commit.
                            # The containment-safe key was still resolved by the store, so
                            # missing bytes count as an already-completed physical delete.
                            pass
                        session.delete(asset)
                        deleted += 1
                return deleted
        finally:
            if deployment_lock is not None:
                deployment_lock.close()


    def _require_profile(self, user_id: int) -> UserProfileView:
        """Return the post-commit profile or report a broken user invariant."""
        profile = self.get_profile(user_id)
        if profile is None:
            raise LookupError("registered user not found")
        return profile

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
        """Apply the persisted NFC and Unicode-whitespace normalization contract."""
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
