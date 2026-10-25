"""Source and immutable audience snapshot operations for User Actor delivery."""

from __future__ import annotations

import json
from urllib.parse import urlparse

from sqlalchemy.orm import Session
from sqlalchemy import select

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
        direct_authorizations: list[dict] | None = None,
    ) -> UserActorOutboundSource:
        """Insert one already-rendered content source and verified audience."""
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
        # Only caller-verified context evidence is a direct grant. The old
        # untyped URLs are never promoted to deliverable recipients here.
        by_endpoint: dict[str, list[dict]] = {}
        for evidence in direct_authorizations or []:
            if (evidence.get('schemaVersion') != 1
                or evidence.get('contextKind') not in {'remote_community_publication', 'personal_feed_reply'}
                or evidence.get('publishedObjectId') != published_object_id
                or not evidence.get('recipientActorId')):
                raise ValueError('invalid direct recipient authorization evidence')
            endpoint = UserActorDeliverySourceRepository.validate_endpoint(
                evidence['targetEndpointUrl'])
            by_endpoint.setdefault(endpoint, []).append(evidence)
        for endpoint, evidence in sorted(by_endpoint.items()):
            snapshot = json.dumps(sorted(evidence, key=lambda x: (x['contextKind'], x['recipientActorId'])),
                                  sort_keys=True, separators=(',', ':'))
            if endpoint in existing_endpoints:
                delivery = session.scalar(select(UserActorOutboundDelivery).where(
                    UserActorOutboundDelivery.source_id == source.id,
                    UserActorOutboundDelivery.target_endpoint_url == endpoint,
                ))
                if delivery is None:
                    raise ValueError('missing follower delivery during direct merge')
                delivery.direct_authorizations_json = snapshot
            else:
                session.add(UserActorOutboundDelivery(
                    source_id=source.id,
                    target_endpoint_url=endpoint,
                    direct_authorizations_json=snapshot,
                    status='pending',
                ))
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
