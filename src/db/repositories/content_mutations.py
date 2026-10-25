"""Durable desired-state and reservation persistence for content mutations."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import and_, or_, select
from .platform_surfaces import discord_copy_query

from ...models import (
    PublishedActivityObject,
    User,
    UserActorContentMutation,
    UserActorFollower,
    LocalCommunity, LocalCommunityThread, LocalCommunityMessage,
    CommunityThreadGroup, CommunityMessageGroup,
    ChannelCommunitySubscription, ChannelSurface, RemoteActor,
    InboundConversationObject,
)
from ...activitypub_identity_urls import (
    build_user_delete_comment_activity_id,
    build_user_delete_post_activity_id,
    build_user_update_comment_activity_id,
    build_user_update_post_activity_id,
)
from .base import BaseRepository
from .platform_delivery_intents import cancel_deleted_source_intents
from .local_community_authored_announces import LocalCommunityAuthoredAnnounceRepository


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
        source_message: "MessageRef | None" = None,
    ) -> str:
        """Admit an edited source with current Community owner proof at SQLite writer."""
        fingerprint = self.fingerprint("update", title, body_markdown)
        with self._immediate_session_provider() as session:
            published = session.scalar(
                select(PublishedActivityObject).where(
                    PublishedActivityObject.object_id == object_id
                )
            )
            if published is None:
                return "missing"
            if source_message is not None:
                from .activitypub_objects import resolve_community_mutation_source
                # Repeat source authorization under the same writer reservation
                # as the desired revision. A forum may rebound after ingress.
                proof = resolve_community_mutation_source(
                    session, source_message, external_guild_id=None,
                    actor_external_id=None,
                )
                if proof is None or proof.object_id != published.object_id:
                    return "ineligible"
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

    def record_desired_delete(
        self, *, object_id: str, source_message: "MessageRef | None" = None,
    ) -> str:
        """Admit terminal source deletion with optional exact Community ref."""
        with self._immediate_session_provider() as session:
            published = session.scalar(
                select(PublishedActivityObject).where(
                    PublishedActivityObject.object_id == object_id
                )
            )
            if published is None:
                return "missing"
            if source_message is not None:
                from .activitypub_objects import resolve_community_mutation_source
                # Repeat source authorization under the same writer reservation
                # as the desired revision. A forum may rebound after ingress.
                proof = resolve_community_mutation_source(
                    session, source_message, external_guild_id=None,
                    actor_external_id=None,
                )
                if proof is None or proof.object_id != published.object_id:
                    return "ineligible"
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
            "content_delete" if published.desired_kind == "delete" else "content_update"
        )
        request = {
            "actorUsername": published.actor_username,
            "publicationContext": published.publication_context,
            "communityActorUrl": published.community_actor_url,
            "conversationRootObjectId": published.conversation_root_object_id,
            "apObjectId": published.object_id,
            "kind": published.kind,
            "title": published.desired_title,
            "bodyMarkdown": published.desired_body_markdown,
            "inReplyToObjectId": published.in_reply_to_object_id,
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
            "publicationContext": request["publicationContext"],
            "communityActorUrl": request["communityActorUrl"],
            "conversationRootObjectId": request["conversationRootObjectId"],
            "apObjectId": request["apObjectId"],
            "kind": request["kind"] if kind == "content_update" else None,
            "title": request["title"] if kind == "content_update" else None,
            "bodyMarkdown": (
                request["bodyMarkdown"] if kind == "content_update" else None
            ),
            "inReplyToObjectId": (
                request["inReplyToObjectId"] if kind == "content_update" else None
            ),
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
        # The gateway renders mutations against canonical typed User Actor
        # activity ids; persistence must not reintroduce retired /users URLs.
        actor_parts = urlsplit(published.actor_url)
        public_origin = f"{actor_parts.scheme}://{actor_parts.netloc}"
        is_comment = (
            "/comments/" in published.object_id or "/comment/" in published.object_id
        )
        if kind == "content_delete":
            activity_id = (
                build_user_delete_comment_activity_id(
                    origin=public_origin,
                    username=published.actor_username,
                    activity_id=operation_id,
                )
                if is_comment
                else build_user_delete_post_activity_id(
                    origin=public_origin,
                    username=published.actor_username,
                    activity_id=operation_id,
                )
            )
        else:
            activity_id = (
                build_user_update_comment_activity_id(
                    origin=public_origin,
                    username=published.actor_username,
                    activity_id=operation_id,
                )
                if is_comment
                else build_user_update_post_activity_id(
                    origin=public_origin,
                    username=published.actor_username,
                    activity_id=operation_id,
                )
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
            request_fingerprint=published.desired_fingerprint
            or hashlib.sha256(
                json.dumps(request, separators=(",", ":"), sort_keys=True).encode()
            ).hexdigest(),
            state="reserved",
        )
        session.add(mutation)
        session.flush()
        return mutation.id

    def reserve_next(
        self, *, object_id: str
    ) -> int | None:
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
            )

    def reconcile_authoritative_update_and_reserve(
        self,
        *,
        object_id: str,
        title: str | None,
        body_markdown: str,
        edited_timestamp: datetime,
        source_message: "MessageRef | None" = None,
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
            if source_message is not None:
                from .activitypub_objects import resolve_community_mutation_source
                proof = resolve_community_mutation_source(
                    session, source_message, external_guild_id=None,
                    actor_external_id=None,
                )
                if proof is None or proof.object_id != published.object_id:
                    return "ineligible"
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
            )
            published.discord_reconciliation_needed = False
            published.discord_reconciliation_next_attempt_at = None
            published.discord_reconciliation_last_error = None
            return outcome

    def claim_due(
        self, *, limit: int, stale_after_seconds: int
    ) -> list[ClaimedContentMutation]:
        """Claim bounded reserved or stale operations for one recovery owner."""
        now = datetime.now(timezone.utc)
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
                            UserActorContentMutation.state.in_(
                                ("reserved", "ambiguous", "canonical_ready")
                            ),
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
            mutation = session.scalar(
                select(UserActorContentMutation).where(
                    UserActorContentMutation.id == mutation_id,
                    UserActorContentMutation.claim_token == claim_token,
                    UserActorContentMutation.state == "dispatching",
                )
            )
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
            published = session.get(
                PublishedActivityObject, mutation.published_object_id
            )
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

    @staticmethod
    def _current_direct_grants(session, published: PublishedActivityObject) -> list[dict]:
        """Build a fresh immutable, evidence-backed audience for one mutation.

        Old Create inbox URLs and caller parameters are not authorization. A
        rebound or revoked community forum produces zero direct deliveries,
        while an exact admitted Personal Feed parent author may receive a reply.
        """
        if published.publication_context == 'community':
            if published.source_channel_surface_id is None:
                return []
            surface = session.get(ChannelSurface, published.source_channel_surface_id)
            if (surface is None or surface.kind != 'forum'
                or surface.connector_id != published.source_connector_id
                or surface.remote_subscription_id is None):
                return []
            subscription = session.get(ChannelCommunitySubscription, surface.remote_subscription_id)
            if (subscription is None or subscription.status != 'accepted'
                or subscription.lemmy_community_actor_id != published.community_actor_url
                or not subscription.community_inbox_url or not subscription.follow_activity_id):
                return []
            return [{
                'schemaVersion': 1,
                'contextKind': 'remote_community_publication',
                'publishedObjectId': published.id,
                'subscriptionId': subscription.id,
                'recipientActorId': subscription.lemmy_community_actor_id,
                'followActivityId': subscription.follow_activity_id,
                'targetEndpointUrl': subscription.community_inbox_url,
                'sourceSurfaceId': surface.id,
            }]
        if published.publication_context == 'personal_feed':
            if not published.in_reply_to_object_id or not published.conversation_root_object_id:
                return []
            parent = session.scalar(select(InboundConversationObject).where(
                InboundConversationObject.object_id == published.in_reply_to_object_id,
                InboundConversationObject.conversation_root_object_id == published.conversation_root_object_id,
                InboundConversationObject.deleted_at.is_(None),
            ))
            root = session.scalar(select(PublishedActivityObject).where(
                PublishedActivityObject.object_id == published.conversation_root_object_id,
                PublishedActivityObject.publication_context == 'personal_feed',
                PublishedActivityObject.deleted_at.is_(None),
            ))
            if parent is None or root is None:
                return []
            actor = session.scalar(select(RemoteActor).where(RemoteActor.actor_url == parent.actor_id))
            if actor is None:
                return []
            endpoint = actor.shared_inbox_url or actor.inbox_url
            return [{
                'schemaVersion': 1,
                'contextKind': 'personal_feed_reply',
                'publishedObjectId': published.id,
                'conversationRootObjectId': root.object_id,
                'parentObjectId': parent.object_id,
                'recipientActorId': parent.actor_id,
                'targetEndpointUrl': endpoint,
            }]
        return []

    @staticmethod
    def _apply_community_canonical_mutation(
        session, *, published: PublishedActivityObject,
        mutation: UserActorContentMutation, request: dict,
        canonical_activity_json: dict, now: datetime,
    ) -> None:
        """Advance the existing Local/Remote canonical source in the author commit.

        The published AP object and the community post/comment are one logical
        source. Advancing only the former would silently strand every physical
        mirror at its old revision after a crash or a zero-recipient Update.
        """
        if published.publication_context != 'community':
            return  # Personal Feed is independently owned.
        if published.kind not in ('post', 'comment') or not published.community_actor_url:
            raise ValueError('invalid authored community publication identity')
        is_post = published.kind == 'post'
        local_model = LocalCommunityThread if is_post else LocalCommunityMessage
        remote_model = CommunityThreadGroup if is_post else CommunityMessageGroup
        local_row = session.scalar(select(local_model).where(
            local_model.ap_object_id == published.object_id))
        remote_row = session.scalar(select(remote_model).where(
            remote_model.ap_object_id == published.object_id))
        if local_row is not None and remote_row is not None:
            raise ValueError('authored AP object has conflicting canonical domains')
        row = local_row if local_row is not None else remote_row
        if row is None:
            # Released-v0 publications can lack provable typed canonical
            # attribution. Never manufacture an arbitrary community row.
            if published.source_channel_surface_id is not None:
                raise ValueError('typed community source lacks its canonical row')
            return
        local_community = None
        if local_row is not None:
            local_community_id = (local_row.local_community_id if is_post else
                session.get(LocalCommunityThread, local_row.local_community_thread_id).local_community_id)
            local_community = session.get(LocalCommunity, local_community_id)
            if (local_community is None or local_community.actor_url != published.community_actor_url):
                raise ValueError('authored Local Community ownership changed')
            if not is_post:
                parent = session.get(LocalCommunityThread, local_row.local_community_thread_id)
                if parent is None or parent.deleted_at is not None:
                    raise ValueError('authored comment has a deleted Local Community parent')
        else:
            if remote_row.community_actor_id != published.community_actor_url:
                raise ValueError('authored Remote Community ownership changed')
            if not is_post:
                parent = session.get(CommunityThreadGroup, remote_row.thread_group_id)
                if (parent is None or parent.deleted_at is not None
                    or parent.community_actor_id != published.community_actor_url):
                    raise ValueError('authored comment has a wrong Remote Community parent')
        if row.deleted_at is not None:
            raise ValueError('authored canonical source was already deleted')

        # A published author revision starts at 1, while the domain's
        # content revision starts at 0. Advance each exactly once under the
        # same writer/CAS transaction instead of guessing historical baselines.
        row.content_revision += 1
        if mutation.kind == 'content_delete':
            row.deleted_at = now
            cancel_deleted_source_intents(
                session, domain='local' if local_row is not None else 'remote',
                kind=published.kind, canonical_id=row.id,
                revision=row.content_revision,
            )
        else:
            snapshot = {
                'schemaVersion': 1, 'kind': published.kind,
                'sourceKind': 'discord_human',
                'apObjectId': published.object_id,
                'title': request.get('title') if is_post else None,
                'bodyMarkdown': str(request.get('bodyMarkdown') or ''),
                'authorActorId': published.actor_url,
                'authorName': published.actor_username,
            }
            if not is_post:
                snapshot['inReplyTo'] = published.in_reply_to_object_id
            row.current_content_json = json.dumps(
                snapshot, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
            )
            row.last_source_update_order_key = request.get('mutationTimestamp')
            row.last_source_update_identity = mutation.activity_id

        # The Group-signed authored mutation shares the same atomic writer
        # boundary as the User Actor activity, but keeps its own immutable
        # signature and per-Follow causal delivery ledger.
        if local_community is not None:
            LocalCommunityAuthoredAnnounceRepository.stage_mutation(
                session, published=published, community=local_community,
                activity_kind='delete' if mutation.kind == 'content_delete' else 'update',
                content_revision=mutation.next_revision,
                activity_json=canonical_activity_json,
            )

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
            published = session.get(
                PublishedActivityObject, mutation.published_object_id
            )
            if (
                published is None
                or published.content_revision != mutation.expected_revision
            ):
                return None
            request = json.loads(mutation.request_json)
            if mutation.kind == "content_delete":
                published.deleted_at = now
            else:
                published.title = request.get("title")
                published.body_markdown = str(request.get("bodyMarkdown") or "")
            published.content_revision = mutation.next_revision
            self._apply_community_canonical_mutation(
                session, published=published, mutation=mutation, request=request,
                canonical_activity_json=canonical_activity_json, now=now,
            )
            if mutation.kind == "content_delete":
                published.desired_kind = "none"
            elif (
                published.desired_kind == "update"
                and published.desired_fingerprint == mutation.request_fingerprint
            ):
                published.desired_kind = "none"
            # Addressing is resolved against current exact source ownership in
            # the same writer transaction as this canonical revision.
            direct_grants = self._current_direct_grants(session, published)
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
                    "activityType": (
                        "Delete" if mutation.kind == "content_delete" else "Update"
                    ),
                    "contentRevision": mutation.next_revision,
                },
                followers=followers,
                direct_authorizations=direct_grants,
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

    def request_discord_reconciliation(
        self, *, channel_id: int, message_id: int, error: str
    ) -> str:
        """Durably request current Discord message-state reconciliation."""
        with self._immediate_session_provider() as session:
            published = session.scalar(
                discord_copy_query(PublishedActivityObject, source=True).where(
                    PublishedActivityObject.discord_channel_id == channel_id,
                    PublishedActivityObject.discord_message_id == message_id,
                )
            )
            if published is None:
                return "missing"
            if published.deleted_at is not None or published.desired_kind == "delete":
                return "deleted"
            published.discord_reconciliation_needed = True
            published.discord_reconciliation_last_error = error[:500]
            published.discord_reconciliation_next_attempt_at = datetime.now(
                timezone.utc
            )
            return "accepted"

    def request_discord_reconciliation_by_source_ref(
        self, *, source: "MessageRef", error: str,
    ) -> str:
        """Park an incomplete Community edit by full physical source identity.

        Raw Discord message ID alone cannot identify a safe reconciliation
        after several threads reuse the same message number. A deleted source
        cannot be reactivated by this request.
        """
        from ...platform_types import MessageRef
        from ...models import PlatformConnector

        if not isinstance(source, MessageRef):
            raise TypeError('exact MessageRef required')
        with self._immediate_session_provider() as session:
            published = session.scalar(
                select(PublishedActivityObject)
                .join(PlatformConnector, PublishedActivityObject.source_connector_id == PlatformConnector.id)
                .where(
                    PlatformConnector.connector_key == source.connector_key,
                    PublishedActivityObject.source_external_container_id == source.external_container_id,
                    PublishedActivityObject.source_external_message_id == source.external_message_id,
                    PublishedActivityObject.publication_context == 'community',
                )
            )
            if published is None:
                return 'missing'
            from .activitypub_objects import resolve_community_mutation_source
            verified = resolve_community_mutation_source(
                session, source, external_guild_id=None, actor_external_id=None,
            )
            if verified is None or verified.object_id != published.object_id:
                return 'ineligible'
            if published.deleted_at is not None or published.desired_kind == 'delete':
                return 'deleted'
            published.discord_reconciliation_needed = True
            published.discord_reconciliation_last_error = error[:500]
            published.discord_reconciliation_next_attempt_at = datetime.now(timezone.utc)
            return 'accepted'

    def due_reconciliations(
        self, *, limit: int
    ) -> list[tuple[int, int, str, str | None, "MessageRef | None"]]:
        """Return exact immediate Discord containers for pending source reads.

        The legacy Personal Feed still uses its numeric channel projection;
        a typed Community post's actual message container is its *thread*,
        not the parent forum stored in `discord_channel_id`.
        """
        from ...models import PlatformConnector
        from ...platform_types import MessageRef

        now = datetime.now(timezone.utc)
        with self.session() as session:
            rows = session.scalars(
                select(PublishedActivityObject)
                .where(
                    PublishedActivityObject.discord_reconciliation_needed.is_(True),
                    or_(
                        PublishedActivityObject.discord_reconciliation_next_attempt_at.is_(None),
                        PublishedActivityObject.discord_reconciliation_next_attempt_at <= now,
                    ),
                )
                .order_by(PublishedActivityObject.id)
                .limit(limit)
            ).all()
            items = []
            for row in rows:
                if (row.publication_context == 'community'
                    and row.source_connector_id is not None
                    and row.source_external_container_id is not None
                    and row.source_external_message_id is not None):
                    connector = session.get(PlatformConnector, row.source_connector_id)
                    if (connector is None or connector.connector_key != 'discord-primary'
                        or not row.source_external_container_id.isdecimal()
                        or not row.source_external_message_id.isdecimal()):
                        # Keep invalid/foreign source evidence pending for an
                        # operator; never substitute the parent forum channel.
                        continue
                    ref = MessageRef(
                        connector.connector_key, row.source_external_container_id,
                        row.source_external_message_id,
                    )
                    items.append((int(ref.external_container_id), int(ref.external_message_id),
                                  row.object_id, row.title, ref))
                elif (row.publication_context == 'personal_feed'
                      and row.discord_channel_id is not None
                      and row.discord_message_id is not None):
                    items.append((int(row.discord_channel_id), int(row.discord_message_id),
                                  row.object_id, row.title, None))
            return items

    def record_reconciliation_failure(self, *, object_id: str, error: str) -> None:
        with self._immediate_session_provider() as session:
            published = session.scalar(
                select(PublishedActivityObject).where(
                    PublishedActivityObject.object_id == object_id
                )
            )
            if published is None:
                return
            published.discord_reconciliation_attempt_count += 1
            published.discord_reconciliation_last_error = error[:500]
            published.discord_reconciliation_next_attempt_at = datetime.now(
                timezone.utc
            ) + timedelta(
                seconds=min(
                    300, 2 ** min(published.discord_reconciliation_attempt_count, 8)
                )
            )

    def clear_reconciliation(self, *, object_id: str) -> None:
        with self._immediate_session_provider() as session:
            published = session.scalar(
                select(PublishedActivityObject).where(
                    PublishedActivityObject.object_id == object_id
                )
            )
            if published is None:
                return
            published.discord_reconciliation_needed = False
            published.discord_reconciliation_next_attempt_at = None
            published.discord_reconciliation_last_error = None
