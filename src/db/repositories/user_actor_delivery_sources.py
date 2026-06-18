"""Source and immutable audience snapshot operations for User Actor delivery."""

from __future__ import annotations

import json
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from ...models import (
    UserActorFollower,
    UserActorOutboundDelivery,
    UserActorOutboundDeliveryFollower,
    UserActorOutboundSource,
    UserActorOutboundSourceMedia,
)


class UserActorDeliverySourceRepository:
    """Build immutable outbound sources inside caller-owned transactions."""

    @staticmethod
    def validate_endpoint(endpoint: str) -> str:
        """Accept only normalized absolute HTTPS inbox endpoints."""
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.netloc or parsed.fragment:
            raise ValueError("invalid follower delivery endpoint")
        return endpoint

    @staticmethod
    def insert_source_graph(
        session: Session,
        *,
        user_id: int,
        logical_version: int,
        activity_id: str,
        profile_snapshot_json: str,
        followers: list[UserActorFollower],
        media_refs: list[tuple[str, int]],
    ) -> UserActorOutboundSource:
        """Insert one profile source and its fixed audience atomically with mutation."""
        source = UserActorOutboundSource(
            user_id=user_id,
            source_kind="profile_update",
            logical_version=logical_version,
            activity_id=activity_id,
            ordering_key=f"profile:{user_id}",
            snapshot_json=profile_snapshot_json,
            state="render_pending",
        )
        session.add(source)
        session.flush()

        UserActorDeliverySourceRepository._insert_audience(session, source.id, followers)
        for slot, asset_id in media_refs:
            session.add(
                UserActorOutboundSourceMedia(
                    source_id=source.id, media_asset_id=asset_id, slot_kind=slot
                )
            )
        session.flush()
        return source

    @staticmethod
    def insert_ready_content_source(
        session: Session,
        *,
        user_id: int,
        source_kind: str,
        logical_version: int,
        activity_id: str,
        published_object_id: int,
        object_id: str,
        activity_json: dict,
        snapshot: dict,
        followers: list[UserActorFollower],
        direct_target_inbox_urls: list[str] | None = None,
    ) -> UserActorOutboundSource:
        """Insert one already-rendered content source and immutable audience."""
        source = UserActorOutboundSource(
            user_id=user_id,
            source_kind=source_kind,
            ordering_key=f"content:{object_id}",
            logical_version=logical_version,
            activity_id=activity_id,
            published_object_id=published_object_id,
            snapshot_json=json.dumps(snapshot, separators=(",", ":"), sort_keys=True),
            activity_json=json.dumps(
                activity_json, separators=(",", ":"), sort_keys=True
            ),
            state="ready",
        )
        session.add(source)
        session.flush()
        UserActorDeliverySourceRepository._insert_audience(session, source.id, followers)
        existing_endpoints = {
            UserActorDeliverySourceRepository.validate_endpoint(
                follower.remote_shared_inbox_url or follower.remote_inbox_url
            )
            for follower in followers
        }
        # Direct target endpoints are sorted and deduplicated so replayed content
        # fanout preserves the current deterministic row identities.
        for endpoint_url in sorted(set(direct_target_inbox_urls or [])):
            endpoint = UserActorDeliverySourceRepository.validate_endpoint(endpoint_url)
            if endpoint not in existing_endpoints:
                session.add(
                    UserActorOutboundDelivery(
                        source_id=source.id,
                        target_endpoint_url=endpoint,
                        status="pending",
                    )
                )
                existing_endpoints.add(endpoint)
        return source

    @staticmethod
    def _insert_audience(
        session: Session, source_id: int, followers: list[UserActorFollower]
    ) -> None:
        """Snapshot accepted follower incarnations with shared-inbox deduplication."""
        grouped: dict[str, list[UserActorFollower]] = {}
        for follower in followers:
            endpoint = UserActorDeliverySourceRepository.validate_endpoint(
                follower.remote_shared_inbox_url or follower.remote_inbox_url
            )
            grouped.setdefault(endpoint, []).append(follower)
        for endpoint in sorted(grouped):
            delivery = UserActorOutboundDelivery(
                source_id=source_id, target_endpoint_url=endpoint, status="pending"
            )
            session.add(delivery)
            session.flush()
            for follower in sorted(grouped[endpoint], key=lambda item: item.id):
                session.add(
                    UserActorOutboundDeliveryFollower(
                        delivery_id=delivery.id,
                        relationship_id_snapshot=follower.id,
                        remote_actor_id=follower.remote_actor_id,
                        snapshotted_endpoint_url=endpoint,
                    )
                )
