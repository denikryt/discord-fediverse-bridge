"""Durable desired-state and reservation persistence for content mutations."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import and_, or_, select

from ...models import (
    PublishedActivityObject,
    User,
    UserActorContentMutation,
    UserActorFollower,
)
from .base import BaseRepository


@dataclass(frozen=True, slots=True)
class ClaimedContentMutation:
    """Describe one exclusively claimed replayable gateway operation."""

    mutation_id: int
    claim_token: str
    operation_id: str
    activity_id: str
    published_object_id: int
    user_id: int
    actor_username: str
    actor_url: str
    kind: str
    expected_revision: int
    next_revision: int
    request: dict


class ContentMutationRepository(BaseRepository):
    """Own desired-state admission, reservations, leases, and canonical commits."""

    def __init__(self, session_provider, immediate_session_provider) -> None:
        """Bind ordinary and writer-reserved transaction providers."""
        super().__init__(session_provider)
        self._immediate_session_provider = immediate_session_provider

    @staticmethod
    def fingerprint(kind: str, title: str | None, body: str | None) -> str:
        """Return a deterministic content-state fingerprint."""
        encoded = json.dumps(
            {"kind": kind, "title": title, "body": body},
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(encoded.encode()).hexdigest()

    def record_desired_update(
        self,
        *,
        object_id: str,
        title: str | None,
        body_markdown: str,
        edited_timestamp: datetime,
    ) -> str:
        """Durably accept a content-relevant Discord edit by external timestamp."""
        fingerprint = self.fingerprint("update", title, body_markdown)
        with self._immediate_session_provider() as session:
            published = session.scalar(
                select(PublishedActivityObject).where(
                    PublishedActivityObject.object_id == object_id
                )
            )
            if published is None:
                return "missing"
            if published.deleted_at is not None or published.desired_kind == "delete":
                return "deleted"
            current_ts = published.desired_edited_timestamp
            if current_ts is not None and current_ts.tzinfo is None:
                current_ts = current_ts.replace(tzinfo=timezone.utc)
            if edited_timestamp.tzinfo is None:
                edited_timestamp = edited_timestamp.replace(tzinfo=timezone.utc)
            if current_ts is not None and edited_timestamp < current_ts:
                return "stale"
            if current_ts is not None and edited_timestamp == current_ts:
                if published.desired_fingerprint == fingerprint:
                    return "duplicate"
                return "conflict"
            if (
                published.title == title
                and published.body_markdown == body_markdown
                and published.desired_kind == "none"
            ):
                return "duplicate"
            published.desired_kind = "update"
            published.desired_title = title
            published.desired_body_markdown = body_markdown
            published.desired_edited_timestamp = edited_timestamp
            published.desired_fingerprint = fingerprint
            return "accepted"

    def record_desired_delete(self, *, object_id: str) -> str:
        """Durably accept terminal deletion independently of edit timestamps."""
        with self._immediate_session_provider() as session:
            published = session.scalar(
                select(PublishedActivityObject).where(
                    PublishedActivityObject.object_id == object_id
                )
            )
            if published is None:
                return "missing"
            if published.deleted_at is not None or published.desired_kind == "delete":
                return "duplicate"
            published.desired_kind = "delete"
            published.desired_title = None
            published.desired_body_markdown = None
            published.desired_fingerprint = self.fingerprint("delete", None, None)
            return "accepted"

    def _reserve_next_in_session(
        self,
        session,
        *,
        published: PublishedActivityObject,
        target_inbox_urls: list[str] | None,
    ) -> int | None:
        """Reserve the current desired transition inside an existing writer transaction."""
        if published.desired_kind == "none":
            return None
        active = session.scalar(
            select(UserActorContentMutation).where(
                UserActorContentMutation.published_object_id == published.id,
                UserActorContentMutation.state.in_(
                    ("reserved", "dispatching", "ambiguous", "canonical_ready")
                ),
            )
        )
        if active is not None:
            return active.id
        user = session.scalar(select(User).where(User.actor_url == published.actor_url))
        if user is None:
            raise ValueError("published object actor is not a registered User Actor")
        kind = (
            "content_delete"
            if published.desired_kind == "delete"
            else "content_update"
        )
        request = {
            "actorUsername": published.actor_username,
            "communityActorUrl": published.community_actor_url,
            "apObjectId": published.object_id,
            "kind": published.kind,
            "title": published.desired_title,
            "bodyMarkdown": published.desired_body_markdown,
            "inReplyToObjectId": published.in_reply_to_object_id,
            "targetInboxUrls": target_inbox_urls,
            "mutationTimestamp": (
                published.desired_edited_timestamp.isoformat()
                if published.desired_edited_timestamp is not None
                else None
            ),
        }
        expected = published.content_revision
        next_revision = expected + 1
        identity_request = {
            "actorUsername": request["actorUsername"],
            "communityActorUrl": request["communityActorUrl"],
            "apObjectId": request["apObjectId"],
            "kind": request["kind"] if kind == "content_update" else None,
            "title": request["title"] if kind == "content_update" else None,
            "bodyMarkdown": (
                request["bodyMarkdown"] if kind == "content_update" else None
            ),
            "inReplyToObjectId": (
                request["inReplyToObjectId"]
                if kind == "content_update"
                else None
            ),
            "targetInboxUrls": request["targetInboxUrls"],
            "mutationTimestamp": request["mutationTimestamp"],
        }
        identity = json.dumps(
            {
                "objectId": published.object_id,
                "kind": kind,
                "expectedRevision": expected,
                "nextRevision": next_revision,
                "request": identity_request,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        operation_id = hashlib.sha256(identity.encode()).hexdigest()
        actor_parts = urlsplit(published.actor_url)
        activity_id = (
            f"{actor_parts.scheme}://{actor_parts.netloc}/users/{published.actor_username}/activities/"
            f"{'delete' if kind == 'content_delete' else 'update'}/{operation_id}"
        )
        request["operationId"] = operation_id
        request["activityId"] = activity_id
        mutation = UserActorContentMutation(
            published_object_id=published.id,
            user_id=user.id,
            kind=kind,
            expected_revision=expected,
            next_revision=next_revision,
            operation_id=operation_id,
            activity_id=activity_id,
            request_json=json.dumps(request, separators=(",", ":"), sort_keys=True),
            request_fingerprint=published.desired_fingerprint or hashlib.sha256(
                json.dumps(request, separators=(",", ":"), sort_keys=True).encode()
            ).hexdigest(),
            state="reserved",
        )
        session.add(mutation)
        session.flush()
        return mutation.id

    def reserve_next(self, *, object_id: str, target_inbox_urls: list[str] | None) -> int | None:
        """Reserve one transition from committed state to current desired state."""
        with self._immediate_session_provider() as session:
            published = session.scalar(
                select(PublishedActivityObject).where(
                    PublishedActivityObject.object_id == object_id
                )
            )
            if published is None:
                return None
            return self._reserve_next_in_session(
                session,
                published=published,
                target_inbox_urls=target_inbox_urls,
            )

    def reconcile_authoritative_update_and_reserve(
        self,
        *,
        object_id: str,
        title: str | None,
        body_markdown: str,
        edited_timestamp: datetime,
        target_inbox_urls: list[str] | None,
    ) -> str:
        """Converge one durable reconciliation item to fetched Discord state atomically."""
        fingerprint = self.fingerprint("update", title, body_markdown)
        with self._immediate_session_provider() as session:
            published = session.scalar(
                select(PublishedActivityObject).where(
                    PublishedActivityObject.object_id == object_id
                )
            )
            if published is None:
                return "missing"
            if not published.discord_reconciliation_needed:
                return "not_needed"
            if published.deleted_at is not None or published.desired_kind == "delete":
                published.discord_reconciliation_needed = False
                published.discord_reconciliation_next_attempt_at = None
                published.discord_reconciliation_last_error = None
                return "deleted"

            current_ts = published.desired_edited_timestamp
            if current_ts is not None and current_ts.tzinfo is None:
                current_ts = current_ts.replace(tzinfo=timezone.utc)
            if edited_timestamp.tzinfo is None:
                edited_timestamp = edited_timestamp.replace(tzinfo=timezone.utc)

            # Never let an older fetched state overwrite a newer admitted edit.
            if current_ts is not None and edited_timestamp < current_ts:
                return "stale"

            if (
                current_ts == edited_timestamp
                and published.desired_fingerprint == fingerprint
            ):
                outcome = "duplicate"
            else:
                # A successful fetch is the authoritative current Discord state.
                # It may replace the exact equal-timestamp ambiguous desired value,
                # but cannot cross a newer timestamp or terminal deletion boundary.
                published.desired_kind = "update"
                published.desired_title = title
                published.desired_body_markdown = body_markdown
                published.desired_edited_timestamp = edited_timestamp
                published.desired_fingerprint = fingerprint
                outcome = "accepted"

            self._reserve_next_in_session(
                session,
                published=published,
                target_inbox_urls=target_inbox_urls,
            )
            published.discord_reconciliation_needed = False
            published.discord_reconciliation_next_attempt_at = None
            published.discord_reconciliation_last_error = None
            return outcome

    def claim_due(self, *, limit: int, stale_after_seconds: int) -> list[ClaimedContentMutation]:
        """Claim bounded reserved or stale operations for one recovery owner."""
        now = datetime.now(timezone.utc)
        stale_before = now - timedelta(seconds=stale_after_seconds)
        claims: list[ClaimedContentMutation] = []
        with self._immediate_session_provider() as session:
            rows = session.execute(
                select(UserActorContentMutation, User, PublishedActivityObject)
                .join(User, User.id == UserActorContentMutation.user_id)
                .join(
                    PublishedActivityObject,
                    PublishedActivityObject.id
                    == UserActorContentMutation.published_object_id,
                )
                .where(
                    or_(
                        and_(
                            UserActorContentMutation.state.in_(("reserved", "ambiguous", "canonical_ready")),
                            or_(
                                UserActorContentMutation.next_attempt_at.is_(None),
                                UserActorContentMutation.next_attempt_at <= now,
                            ),
                        ),
                        and_(
                            UserActorContentMutation.state == "dispatching",
                            UserActorContentMutation.lease_expires_at <= now,
                        ),
                    )
                )
                .order_by(UserActorContentMutation.id)
                .limit(limit)
            ).all()
            for mutation, user, _published in rows:
                token = secrets.token_hex(16)
                mutation.claim_token = token
                mutation.claimed_at = now
                mutation.lease_expires_at = now + timedelta(seconds=stale_after_seconds)
                if mutation.state != "canonical_ready":
                    mutation.state = "dispatching"
                    mutation.side_effect_classification = "not_dispatched"
                    mutation.attempt_count += 1
                claims.append(
                    ClaimedContentMutation(
                        mutation_id=mutation.id,
                        claim_token=token,
                        operation_id=mutation.operation_id,
                        activity_id=mutation.activity_id,
                        published_object_id=mutation.published_object_id,
                        user_id=mutation.user_id,
                        actor_username=user.activitypub_username,
                        actor_url=user.actor_url,
                        kind=mutation.kind,
                        expected_revision=mutation.expected_revision,
                        next_revision=mutation.next_revision,
                        request=json.loads(mutation.request_json),
                    )
                )
        return claims

    def mark_send_possible(self, mutation_id: int, claim_token: str) -> bool:
        """Persist the boundary after which a remote side effect is possible."""
        with self.session() as session:
            mutation = session.scalar(select(UserActorContentMutation).where(
                UserActorContentMutation.id == mutation_id,
                UserActorContentMutation.claim_token == claim_token,
                UserActorContentMutation.state == "dispatching",
            ))
            if mutation is None:
                return False
            mutation.side_effect_classification = "send_possible"
            return True

    def record_canonical_result(
        self, mutation_id: int, claim_token: str, activity_json: dict
    ) -> bool:
        """Persist one canonical result before the final local commit."""
        with self.session() as session:
            mutation = session.scalar(
                select(UserActorContentMutation).where(
                    UserActorContentMutation.id == mutation_id,
                    UserActorContentMutation.claim_token == claim_token,
                    UserActorContentMutation.state == "dispatching",
                )
            )
            if mutation is None:
                return False
            mutation.canonical_activity_json = json.dumps(
                activity_json, separators=(",", ":"), sort_keys=True
            )
            mutation.state = "canonical_ready"
            mutation.side_effect_classification = "canonical_received"
            mutation.last_error = None
            mutation.next_attempt_at = None
            return True

    def record_retryable_failure(
        self, mutation_id: int, claim_token: str, error: str
    ) -> None:
        """Release an ambiguous gateway attempt for stable-identity replay."""
        with self.session() as session:
            mutation = session.scalar(
                select(UserActorContentMutation).where(
                    UserActorContentMutation.id == mutation_id,
                    UserActorContentMutation.claim_token == claim_token,
                    UserActorContentMutation.state == "dispatching",
                )
            )
            if mutation is None:
                return
            mutation.state = "ambiguous"
            mutation.side_effect_classification = "send_possible"
            mutation.claim_token = None
            mutation.claimed_at = None
            mutation.lease_expires_at = None
            mutation.last_error = error[:500]
            mutation.next_attempt_at = datetime.now(timezone.utc) + timedelta(
                seconds=min(300, 2 ** min(mutation.attempt_count, 8))
            )

    def abort_local_validation(
        self, mutation_id: int, claim_token: str, error: str
    ) -> None:
        """Abort a pre-send invalid transition without advancing revision."""
        with self.session() as session:
            mutation = session.scalar(
                select(UserActorContentMutation).where(
                    UserActorContentMutation.id == mutation_id,
                    UserActorContentMutation.claim_token == claim_token,
                    UserActorContentMutation.state == "dispatching",
                )
            )
            if mutation is None:
                return
            mutation.state = "aborted_pre_send"
            mutation.side_effect_classification = "not_dispatched"
            mutation.claim_token = None
            mutation.claimed_at = None
            mutation.lease_expires_at = None
            mutation.last_error = error[:500]
            mutation.next_attempt_at = None
            published = session.get(PublishedActivityObject, mutation.published_object_id)
            if (
                published is not None
                and published.desired_kind == "update"
                and published.desired_fingerprint == mutation.request_fingerprint
            ):
                published.desired_kind = "none"
                published.desired_title = None
                published.desired_body_markdown = None
                published.desired_edited_timestamp = None
                published.desired_fingerprint = None

    def commit_canonical(
        self,
        *,
        mutation_id: int,
        claim_token: str,
        canonical_activity_json: dict,
        delivery_repository: object,
    ) -> tuple[int, str] | None:
        """Atomically apply canonical state and create its follower source."""
        now = datetime.now(timezone.utc)
        with self._immediate_session_provider() as session:
            mutation = session.scalar(
                select(UserActorContentMutation).where(
                    UserActorContentMutation.id == mutation_id,
                    UserActorContentMutation.claim_token == claim_token,
                    UserActorContentMutation.state == "canonical_ready",
                )
            )
            if mutation is None:
                return None
            published = session.get(PublishedActivityObject, mutation.published_object_id)
            if published is None or published.content_revision != mutation.expected_revision:
                return None
            request = json.loads(mutation.request_json)
            if mutation.kind == "content_delete":
                published.deleted_at = now
            else:
                published.title = request.get("title")
                published.body_markdown = str(request.get("bodyMarkdown") or "")
            published.content_revision = mutation.next_revision
            if mutation.kind == "content_delete":
                published.desired_kind = "none"
            elif (
                published.desired_kind == "update"
                and published.desired_fingerprint == mutation.request_fingerprint
            ):
                published.desired_kind = "none"
            followers = list(
                session.scalars(
                    select(UserActorFollower).where(
                        UserActorFollower.user_id == mutation.user_id,
                        UserActorFollower.status == "accepted",
                    )
                )
            )
            source = delivery_repository.insert_ready_content_source(
                session,
                user_id=mutation.user_id,
                source_kind=mutation.kind,
                logical_version=mutation.next_revision,
                activity_id=mutation.activity_id,
                published_object_id=published.id,
                object_id=published.object_id,
                activity_json=canonical_activity_json,
                snapshot={
                    "schemaVersion": 1,
                    "objectId": published.object_id,
                    "activityType": "Delete" if mutation.kind == "content_delete" else "Update",
                    "contentRevision": mutation.next_revision,
                },
                followers=followers,
            )
            mutation.state = "committed"
            mutation.claim_token = None
            mutation.claimed_at = None
            mutation.lease_expires_at = None
            mutation.next_attempt_at = None
            return source.id, published.object_id

    def load_canonical_ready(self, mutation_id: int, claim_token: str) -> dict | None:
        """Load canonical JSON for one claimed canonical-ready reservation."""
        with self.session() as session:
            mutation = session.scalar(
                select(UserActorContentMutation).where(
                    UserActorContentMutation.id == mutation_id,
                    UserActorContentMutation.claim_token == claim_token,
                    UserActorContentMutation.state == "canonical_ready",
                )
            )
            if mutation is None or mutation.canonical_activity_json is None:
                return None
            return json.loads(mutation.canonical_activity_json)


    def request_discord_reconciliation(self, *, channel_id: int, message_id: int, error: str) -> str:
        """Durably request current Discord message-state reconciliation."""
        with self._immediate_session_provider() as session:
            published = session.scalar(select(PublishedActivityObject).where(
                PublishedActivityObject.discord_channel_id == channel_id,
                PublishedActivityObject.discord_message_id == message_id,
            ))
            if published is None:
                return "missing"
            if published.deleted_at is not None or published.desired_kind == "delete":
                return "deleted"
            published.discord_reconciliation_needed = True
            published.discord_reconciliation_last_error = error[:500]
            published.discord_reconciliation_next_attempt_at = datetime.now(timezone.utc)
            return "accepted"

    def due_reconciliations(self, *, limit: int) -> list[tuple[int, int, str, str | None]]:
        now = datetime.now(timezone.utc)
        with self.session() as session:
            rows = session.scalars(select(PublishedActivityObject).where(
                PublishedActivityObject.discord_reconciliation_needed.is_(True),
                or_(PublishedActivityObject.discord_reconciliation_next_attempt_at.is_(None), PublishedActivityObject.discord_reconciliation_next_attempt_at <= now),
            ).order_by(PublishedActivityObject.id).limit(limit)).all()
            return [(int(r.discord_channel_id), int(r.discord_message_id), r.object_id, r.title) for r in rows if r.discord_channel_id is not None and r.discord_message_id is not None]

    def record_reconciliation_failure(self, *, object_id: str, error: str) -> None:
        with self._immediate_session_provider() as session:
            published = session.scalar(select(PublishedActivityObject).where(PublishedActivityObject.object_id == object_id))
            if published is None:
                return
            published.discord_reconciliation_attempt_count += 1
            published.discord_reconciliation_last_error = error[:500]
            published.discord_reconciliation_next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=min(300, 2 ** min(published.discord_reconciliation_attempt_count, 8)))

    def clear_reconciliation(self, *, object_id: str) -> None:
        with self._immediate_session_provider() as session:
            published = session.scalar(select(PublishedActivityObject).where(PublishedActivityObject.object_id == object_id))
            if published is None:
                return
            published.discord_reconciliation_needed = False
            published.discord_reconciliation_next_attempt_at = None
            published.discord_reconciliation_last_error = None
