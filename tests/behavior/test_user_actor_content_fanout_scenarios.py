"""Behavior scenarios for Stage 5 canonical content follower fanout."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.content_publish_service import ContentPublishService
from src.db import Database
from src.fedify_gateway_client import PublishContentResult
from src.models import UserActorOutboundDelivery, UserActorOutboundSource
from src.user_actor_delivery import UserActorDeliveryService


class AllowAllPolicy:
    """Allow all endpoints for deterministic transport scenarios."""

    def federation_decision(self, _target: str) -> SimpleNamespace:
        """Return one allowed policy decision."""
        return SimpleNamespace(allowed=True)


class WakeRecorder:
    """Record post-commit worker wakeups."""

    def __init__(self) -> None:
        self.count = 0

    def wake(self) -> None:
        """Record one committed source notification."""
        self.count += 1


@pytest.mark.asyncio
async def test_remote_publish_commits_exact_canonical_create_and_shared_inbox_once(
    tmp_path,
) -> None:
    """One Discord post keeps community delivery and adds one deduplicated follower source."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    database.migrate()
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://bridge.example/actors/users/alice",
        inbox_url="https://bridge.example/actors/users/alice/inbox",
        outbox_url="https://bridge.example/actors/users/alice/outbox",
        followers_url="https://bridge.example/actors/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    with database.immediate_session() as session:
        for suffix in ("a", "b"):
            database.user_actor_followers.create_or_refresh(
                session,
                user_id=user.id,
                remote_actor_id=f"https://remote.example/users/{suffix}",
                remote_inbox_url=f"https://remote.example/users/{suffix}/inbox",
                remote_shared_inbox_url="https://remote.example/inbox",
                follow_activity_id=f"https://remote.example/follows/{suffix}",
            )
    canonical = {
        "id": "https://bridge.example/actors/users/alice/activities/create/posts/1",
        "type": "Create",
        "actor": user.actor_url,
        "object": {"id": "https://bridge.example/posts/1", "type": "Page"},
    }
    gateway = AsyncMock()
    gateway.publish_content.return_value = PublishContentResult(
        activity_id=canonical["id"],
        object_id=canonical["object"]["id"],
        community_actor_url="https://remote.example/c/group",
        activity_json=canonical,
    )
    wake = WakeRecorder()
    service = ContentPublishService(
        database=database,
        fedify_gateway=gateway,
        bridge_prefix="[bridge]",
        bridge_policy_service=AllowAllPolicy(),
        delivery_worker=wake,
    )
    author = SimpleNamespace(id=1, display_name="Alice", name="Alice")
    message = SimpleNamespace(id=11, author=author, content="hello", reply=AsyncMock())
    thread = SimpleNamespace(id=22, parent_id=33, name="Topic")

    result = await service.publish_post_to_community(
        thread=thread,
        starter_message=message,
        community_actor_url="https://remote.example/c/group",
        publish_call=gateway.publish_content,
    )

    assert result.status == "published"
    assert wake.count == 1
    with database.session() as session:
        source = session.scalar(select(UserActorOutboundSource))
        deliveries = list(session.scalars(select(UserActorOutboundDelivery)))
        assert source.source_kind == "content_create"
        assert source.activity_id == canonical["id"]
        assert (
            source.activity_json
            == '{"actor":"https://bridge.example/actors/users/alice","id":"https://bridge.example/actors/users/alice/activities/create/posts/1","object":{"id":"https://bridge.example/posts/1","type":"Page"},"type":"Create"}'
        )
        assert [row.target_endpoint_url for row in deliveries] == [
            "https://remote.example/inbox"
        ]


@pytest.mark.asyncio
async def test_update_is_reserved_once_and_duplicate_discord_edit_is_noop(
    tmp_path,
) -> None:
    """Stable desired evidence creates one revision and one immutable follower source."""
    from datetime import datetime, timezone
    from src.fedify_gateway_client import CanonicalMutationResult
    from src.models import PublishedActivityObject, UserActorContentMutation

    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    database.migrate()
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://bridge.example/actors/users/alice",
        inbox_url="https://bridge.example/actors/users/alice/inbox",
        outbox_url="https://bridge.example/actors/users/alice/outbox",
        followers_url="https://bridge.example/actors/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    database.activitypub_objects.create_published_activity_object(
        actor_username=user.activitypub_username,
        actor_url=user.actor_url,
        community_actor_url="https://remote.example/c/group",
        activity_id="https://bridge.example/actors/users/alice/activities/create/posts/1",
        object_id="https://bridge.example/posts/1",
        kind="post",
        title="Topic",
        body_markdown="old",
        in_reply_to_object_id=None,
        discord_channel_id=10,
        discord_message_id=11,
    )

    class MutationGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def update_content(self, request):
            self.calls += 1
            return CanonicalMutationResult(
                activity_id=request.activity_id,
                object_id=request.ap_object_id,
                activity_json={
                    "id": request.activity_id,
                    "type": "Update",
                    "actor": user.actor_url,
                    "object": {
                        "id": request.ap_object_id,
                        "content": request.body_markdown,
                    },
                },
            )

        async def delete_content(self, request):
            raise AssertionError("delete not expected")

        async def send_user_actor_deliveries(self, **_kwargs):
            return []

    gateway = MutationGateway()
    service = UserActorDeliveryService(database, gateway, AllowAllPolicy())
    timestamp = datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc)

    first = await service.submit_update(
        object_id="https://bridge.example/posts/1",
        title="Topic",
        body_markdown="new",
        edited_timestamp=timestamp,
    )
    duplicate = await service.submit_update(
        object_id="https://bridge.example/posts/1",
        title="Topic",
        body_markdown="new",
        edited_timestamp=timestamp,
    )

    assert first == "accepted"
    assert duplicate == "duplicate"
    assert gateway.calls == 1
    with database.session() as session:
        published = session.scalar(select(PublishedActivityObject))
        mutations = list(session.scalars(select(UserActorContentMutation)))
        sources = list(
            session.scalars(
                select(UserActorOutboundSource).where(
                    UserActorOutboundSource.source_kind == "content_update"
                )
            )
        )
        assert published.content_revision == 2
        assert published.body_markdown == "new"
        assert published.desired_kind == "none"
        assert [row.state for row in mutations] == ["committed"]
        assert len(sources) == 1
        assert sources[0].activity_id == mutations[0].activity_id


@pytest.mark.asyncio
async def test_delete_waits_for_ambiguous_update_then_commits_terminal_revision(
    tmp_path,
) -> None:
    """A durable Delete is not lost while an earlier stable-identity Update retries."""
    from datetime import datetime, timezone
    from src.fedify_gateway_client import CanonicalMutationResult
    from src.models import PublishedActivityObject, UserActorContentMutation

    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    database.migrate()
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://bridge.example/actors/users/alice",
        inbox_url="https://bridge.example/actors/users/alice/inbox",
        outbox_url="https://bridge.example/actors/users/alice/outbox",
        followers_url="https://bridge.example/actors/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    database.activitypub_objects.create_published_activity_object(
        actor_username=user.activitypub_username,
        actor_url=user.actor_url,
        community_actor_url="https://remote.example/c/group",
        activity_id="https://bridge.example/actors/users/alice/activities/create/posts/1",
        object_id="https://bridge.example/posts/1",
        kind="post",
        title="Topic",
        body_markdown="old",
        in_reply_to_object_id=None,
        discord_channel_id=10,
        discord_message_id=11,
    )

    class FlakyGateway:
        def __init__(self) -> None:
            self.update_attempts = 0
            self.delete_attempts = 0

        async def update_content(self, request):
            self.update_attempts += 1
            if self.update_attempts == 1:
                raise RuntimeError("ambiguous timeout")
            return CanonicalMutationResult(
                request.activity_id,
                request.ap_object_id,
                {
                    "id": request.activity_id,
                    "type": "Update",
                    "actor": user.actor_url,
                    "object": request.ap_object_id,
                },
            )

        async def delete_content(self, request):
            self.delete_attempts += 1
            return CanonicalMutationResult(
                request.activity_id,
                request.ap_object_id,
                {
                    "id": request.activity_id,
                    "type": "Delete",
                    "actor": user.actor_url,
                    "object": request.ap_object_id,
                },
            )

        async def send_user_actor_deliveries(self, **_kwargs):
            return []

    gateway = FlakyGateway()
    service = UserActorDeliveryService(database, gateway, AllowAllPolicy())
    await service.submit_update(
        object_id="https://bridge.example/posts/1",
        title="Topic",
        body_markdown="new",
        edited_timestamp=datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc),
    )
    assert (
        await service.submit_delete(object_id="https://bridge.example/posts/1")
        == "accepted"
    )
    # Make the stable-identity retry due without waiting for wall-clock backoff.
    with database.session() as session:
        mutation = session.scalar(select(UserActorContentMutation))
        mutation.next_attempt_at = None
    await service.drain_mutations(limit=10)
    await service.drain_mutations(limit=10)

    with database.session() as session:
        published = session.scalar(select(PublishedActivityObject))
        states = list(
            session.scalars(
                select(UserActorContentMutation.state).order_by(
                    UserActorContentMutation.id
                )
            )
        )
        assert published.deleted_at is not None
        assert published.content_revision == 3
        assert states == ["committed", "committed"]
    assert gateway.update_attempts == 2
    assert gateway.delete_attempts == 1


@pytest.mark.asyncio
async def test_python_reservation_uses_gateway_user_activity_namespace(
    tmp_path,
) -> None:
    from datetime import datetime, timezone
    from src.models import UserActorContentMutation

    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    database.migrate()
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://bridge.example/actors/users/alice",
        inbox_url="https://bridge.example/actors/users/alice/inbox",
        outbox_url="https://bridge.example/actors/users/alice/outbox",
        followers_url="https://bridge.example/actors/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    database.activitypub_objects.create_published_activity_object(
        actor_username="alice",
        actor_url=user.actor_url,
        community_actor_url="https://remote.example/c/group",
        activity_id="https://bridge.example/actors/users/alice/activities/create/posts/1",
        object_id="https://bridge.example/posts/1",
        kind="post",
        title="Topic",
        body_markdown="old",
        in_reply_to_object_id=None,
        discord_channel_id=10,
        discord_message_id=11,
    )
    database.content_mutations.record_desired_update(
        object_id="https://bridge.example/posts/1",
        title="Topic",
        body_markdown="new",
        edited_timestamp=datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc),
    )
    database.content_mutations.reserve_next(
        object_id="https://bridge.example/posts/1", target_inbox_urls=[]
    )
    with database.session() as session:
        mutation = session.scalar(select(UserActorContentMutation))
        assert mutation.activity_id.startswith(
            "https://bridge.example/actors/users/alice/activities/update/"
        )


@pytest.mark.asyncio
async def test_pre_send_invalid_update_does_not_block_later_delete(tmp_path) -> None:
    from datetime import datetime, timezone
    from src.fedify_gateway_client import CanonicalMutationResult
    from src.models import PublishedActivityObject, UserActorContentMutation

    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    database.migrate()
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://bridge.example/actors/users/alice",
        inbox_url="https://bridge.example/actors/users/alice/inbox",
        outbox_url="https://bridge.example/actors/users/alice/outbox",
        followers_url="https://bridge.example/actors/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    database.activitypub_objects.create_published_activity_object(
        actor_username="alice",
        actor_url=user.actor_url,
        community_actor_url="https://remote.example/c/group",
        activity_id="https://bridge.example/actors/users/alice/activities/create/posts/1",
        object_id="https://bridge.example/posts/1",
        kind="unsupported",
        title="Topic",
        body_markdown="old",
        in_reply_to_object_id=None,
        discord_channel_id=10,
        discord_message_id=11,
    )

    class Gateway:
        async def update_content(self, request):
            raise AssertionError("pre-send validation must stop update")

        async def delete_content(self, request):
            return CanonicalMutationResult(
                request.activity_id,
                request.ap_object_id,
                {
                    "id": request.activity_id,
                    "type": "Delete",
                    "actor": user.actor_url,
                    "object": request.ap_object_id,
                },
            )

        async def send_user_actor_deliveries(self, **kwargs):
            return []

    service = UserActorDeliveryService(database, Gateway(), AllowAllPolicy())
    assert (
        await service.submit_update(
            object_id="https://bridge.example/posts/1",
            title="Topic",
            body_markdown="bad",
            edited_timestamp=datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc),
        )
        == "accepted"
    )
    assert (
        await service.submit_delete(object_id="https://bridge.example/posts/1")
        == "accepted"
    )
    with database.session() as session:
        published = session.scalar(select(PublishedActivityObject))
        states = list(
            session.scalars(
                select(UserActorContentMutation.state).order_by(
                    UserActorContentMutation.id
                )
            )
        )
        assert published.deleted_at is not None
        assert published.content_revision == 2
        assert states == ["aborted_pre_send", "committed"]


@pytest.mark.asyncio
async def test_durable_partial_edit_reconciles_after_restart(tmp_path) -> None:
    from datetime import datetime, timezone
    from src.fedify_gateway_client import CanonicalMutationResult
    from src.models import PublishedActivityObject

    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    database.migrate()
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://bridge.example/actors/users/alice",
        inbox_url="https://bridge.example/actors/users/alice/inbox",
        outbox_url="https://bridge.example/actors/users/alice/outbox",
        followers_url="https://bridge.example/actors/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    database.activitypub_objects.create_published_activity_object(
        actor_username="alice",
        actor_url=user.actor_url,
        community_actor_url="https://remote.example/c/group",
        activity_id="https://bridge.example/actors/users/alice/activities/create/posts/1",
        object_id="https://bridge.example/posts/1",
        kind="post",
        title=None,
        body_markdown="old",
        in_reply_to_object_id=None,
        discord_channel_id=10,
        discord_message_id=11,
    )
    database.content_mutations.request_discord_reconciliation(
        channel_id=10, message_id=11, error="temporary fetch failure"
    )

    class Reader:
        async def fetch_message_state(self, channel_id, message_id):
            return "present", SimpleNamespace(
                content="new",
                edited_at=datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc),
            )

    class Gateway:
        calls = 0

        async def update_content(self, request):
            self.calls += 1
            return CanonicalMutationResult(
                request.activity_id,
                request.ap_object_id,
                {
                    "id": request.activity_id,
                    "type": "Update",
                    "actor": user.actor_url,
                    "object": request.ap_object_id,
                },
            )

        async def delete_content(self, request):
            raise AssertionError

        async def send_user_actor_deliveries(self, **kwargs):
            return []

    gateway = Gateway()
    recovered = UserActorDeliveryService(
        database, gateway, AllowAllPolicy(), discord_message_reader=Reader()
    )
    await recovered.drain_once()
    with database.session() as session:
        published = session.scalar(select(PublishedActivityObject))
        assert published.body_markdown == "new"
        assert published.discord_reconciliation_needed is False
        assert published.content_revision == 2
    assert gateway.calls == 1


@pytest.mark.asyncio
async def test_reconciliation_conflict_converges_to_authoritative_fetch(
    tmp_path,
) -> None:
    """Equal timestamps converge to fetched current Discord state exactly once."""
    from datetime import datetime, timezone
    from src.fedify_gateway_client import CanonicalMutationResult
    from src.models import (
        PublishedActivityObject,
        UserActorContentMutation,
        UserActorOutboundSource,
    )

    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    database.migrate()
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://bridge.example/actors/users/alice",
        inbox_url="https://bridge.example/actors/users/alice/inbox",
        outbox_url="https://bridge.example/actors/users/alice/outbox",
        followers_url="https://bridge.example/actors/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    database.activitypub_objects.create_published_activity_object(
        actor_username="alice",
        actor_url=user.actor_url,
        community_actor_url="https://remote.example/c/group",
        activity_id="https://bridge.example/actors/users/alice/activities/create/posts/1",
        object_id="https://bridge.example/posts/1",
        kind="post",
        title=None,
        body_markdown="old",
        in_reply_to_object_id=None,
        discord_channel_id=10,
        discord_message_id=11,
    )
    timestamp = datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc)
    assert (
        database.content_mutations.record_desired_update(
            object_id="https://bridge.example/posts/1",
            title=None,
            body_markdown="first",
            edited_timestamp=timestamp,
        )
        == "accepted"
    )
    database.content_mutations.request_discord_reconciliation(
        channel_id=10, message_id=11, error="equal timestamp conflict"
    )

    class Reader:
        async def fetch_message_state(self, channel_id, message_id):
            return "present", SimpleNamespace(content="second", edited_at=timestamp)

    class Gateway:
        calls = 0

        async def update_content(self, request):
            self.calls += 1
            return CanonicalMutationResult(
                request.activity_id,
                request.ap_object_id,
                {
                    "id": request.activity_id,
                    "type": "Update",
                    "actor": user.actor_url,
                    "object": request.ap_object_id,
                },
            )

        async def delete_content(self, request):
            raise AssertionError

        async def send_user_actor_deliveries(self, **kwargs):
            return []

    gateway = Gateway()
    service = UserActorDeliveryService(
        database, gateway, AllowAllPolicy(), discord_message_reader=Reader()
    )
    await service.drain_once()
    await service.drain_once()

    with database.session() as session:
        published = session.scalar(select(PublishedActivityObject))
        mutations = list(session.scalars(select(UserActorContentMutation)))
        sources = list(session.scalars(select(UserActorOutboundSource)))
        assert published.body_markdown == "second"
        assert published.discord_reconciliation_needed is False
        assert published.content_revision == 2
        assert len(mutations) == 1
        assert mutations[0].state == "committed"
        assert len(sources) == 1
    assert gateway.calls == 1


@pytest.mark.asyncio
async def test_authoritative_reconciliation_does_not_overwrite_newer_update_or_delete(
    tmp_path,
) -> None:
    """A stale fetched result cannot cross a newer desired or terminal boundary."""
    from datetime import datetime, timezone
    from src.models import PublishedActivityObject, UserActorContentMutation

    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    database.migrate()
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://bridge.example/actors/users/alice",
        inbox_url="https://bridge.example/actors/users/alice/inbox",
        outbox_url="https://bridge.example/actors/users/alice/outbox",
        followers_url="https://bridge.example/actors/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    database.activitypub_objects.create_published_activity_object(
        actor_username="alice",
        actor_url=user.actor_url,
        community_actor_url="https://remote.example/c/group",
        activity_id="https://bridge.example/actors/users/alice/activities/create/posts/1",
        object_id="https://bridge.example/posts/1",
        kind="post",
        title=None,
        body_markdown="old",
        in_reply_to_object_id=None,
        discord_channel_id=10,
        discord_message_id=11,
    )
    old_ts = datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc)
    new_ts = datetime(2026, 6, 16, 10, 1, tzinfo=timezone.utc)
    assert (
        database.content_mutations.record_desired_update(
            object_id="https://bridge.example/posts/1",
            title=None,
            body_markdown="old-conflict",
            edited_timestamp=old_ts,
        )
        == "accepted"
    )
    database.content_mutations.request_discord_reconciliation(
        channel_id=10, message_id=11, error="conflict"
    )
    assert (
        database.content_mutations.record_desired_update(
            object_id="https://bridge.example/posts/1",
            title=None,
            body_markdown="newer",
            edited_timestamp=new_ts,
        )
        == "accepted"
    )

    outcome = database.content_mutations.reconcile_authoritative_update_and_reserve(
        object_id="https://bridge.example/posts/1",
        title=None,
        body_markdown="stale-fetched",
        edited_timestamp=old_ts,
        target_inbox_urls=[],
    )
    assert outcome == "stale"
    with database.session() as session:
        published = session.scalar(select(PublishedActivityObject))
        assert published.desired_body_markdown == "newer"
        assert published.discord_reconciliation_needed is True
        assert session.scalar(select(UserActorContentMutation)) is None

    assert (
        database.content_mutations.record_desired_delete(
            object_id="https://bridge.example/posts/1"
        )
        == "accepted"
    )
    outcome = database.content_mutations.reconcile_authoritative_update_and_reserve(
        object_id="https://bridge.example/posts/1",
        title=None,
        body_markdown="stale-fetched",
        edited_timestamp=old_ts,
        target_inbox_urls=[],
    )
    assert outcome == "deleted"
    with database.session() as session:
        published = session.scalar(select(PublishedActivityObject))
        assert published.desired_kind == "delete"
        assert published.discord_reconciliation_needed is False


@pytest.mark.asyncio
async def test_local_community_reconciliation_preserves_policy_filtered_targets(
    tmp_path,
) -> None:
    """Restart recovery never expands a local-community mutation target set."""
    from datetime import datetime, timezone
    from src.local_communities.service import LocalCommunityService
    from src.models import UserActorContentMutation

    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    database.migrate()
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://bridge.example/actors/users/alice",
        inbox_url="https://bridge.example/actors/users/alice/inbox",
        outbox_url="https://bridge.example/actors/users/alice/outbox",
        followers_url="https://bridge.example/actors/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    LocalCommunityService(
        database=database,
        base_url="https://bridge.example",
        keypair_generator=lambda: ("community-public", "community-private"),
    ).create_local_community(
        discord_guild_id=1,
        discord_forum_channel_id=2,
        slug="group",
        name="Group",
        description="Group",
        created_by_discord_user_id="1",
    )
    community = database.local_communities.get_local_community_by_slug("group")
    allowed = "https://allowed.example/inbox"
    blocked = "https://blocked.example/inbox"
    for index, inbox in enumerate((allowed, blocked)):
        database.remote_subscribers.create_remote_subscriber(
            local_community_id=community.id,
            remote_actor_id=f"https://remote.example/u/{index}",
            remote_inbox_url=inbox,
            follow_activity_id=f"https://remote.example/follows/{index}",
        )
    database.activitypub_objects.create_published_activity_object(
        actor_username="alice",
        actor_url=user.actor_url,
        community_actor_url=community.actor_url,
        activity_id="https://bridge.example/actors/users/alice/activities/create/posts/1",
        object_id="https://bridge.example/posts/1",
        kind="post",
        title="Topic",
        body_markdown="old",
        in_reply_to_object_id=None,
        discord_channel_id=10,
        discord_message_id=11,
    )
    database.content_mutations.request_discord_reconciliation(
        channel_id=10, message_id=11, error="temporary fetch failure"
    )

    class SelectivePolicy:
        def federation_decision(self, target):
            return SimpleNamespace(allowed=target != blocked)

    class Reader:
        async def fetch_message_state(self, channel_id, message_id):
            return "present", SimpleNamespace(
                content="new",
                edited_at=datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc),
            )

    service = UserActorDeliveryService(
        database, AsyncMock(), SelectivePolicy(), discord_message_reader=Reader()
    )
    await service.drain_reconciliations()

    with database.session() as session:
        mutation = session.scalar(select(UserActorContentMutation))
        assert mutation is not None
        assert __import__("json").loads(mutation.request_json)["targetInboxUrls"] == [
            allowed
        ]


def _content_delivery_lifecycle(tmp_path):
    """Build one object with Create, Update, and Delete follower sources."""
    from src.models import PublishedActivityObject, UserActorFollower

    database = Database(f"sqlite:///{tmp_path / 'ordering.db'}")
    database.create_all()
    database.migrate()
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://bridge.example/actors/users/alice",
        inbox_url="https://bridge.example/actors/users/alice/inbox",
        outbox_url="https://bridge.example/actors/users/alice/outbox",
        followers_url="https://bridge.example/actors/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    with database.immediate_session() as session:
        database.user_actor_followers.create_or_refresh(
            session,
            user_id=user.id,
            remote_actor_id="https://remote.example/users/a",
            remote_inbox_url="https://remote.example/inbox",
            remote_shared_inbox_url=None,
            follow_activity_id="https://remote.example/follows/a",
        )
    database.activitypub_objects.create_published_activity_object(
        actor_username="alice",
        actor_url=user.actor_url,
        community_actor_url="https://remote.example/c/group",
        activity_id="https://bridge.example/actors/users/alice/activities/create/posts/1",
        object_id="https://bridge.example/posts/1",
        kind="post",
        title="Topic",
        body_markdown="body",
        in_reply_to_object_id=None,
        discord_channel_id=10,
        discord_message_id=11,
    )
    with database.immediate_session() as session:
        published = session.scalar(select(PublishedActivityObject))
        followers = list(session.scalars(select(UserActorFollower)))
        sources = []
        for version, kind in enumerate(
            ("content_create", "content_update", "content_delete"), start=1
        ):
            source = database.user_actor_deliveries.insert_ready_content_source(
                session,
                user_id=user.id,
                source_kind=kind,
                logical_version=version,
                activity_id=f"https://bridge.example/actors/users/alice/activities/update/profile/{version}",
                published_object_id=published.id,
                object_id=published.object_id,
                activity_json={
                    "id": f"https://bridge.example/actors/users/alice/activities/update/profile/{version}",
                    "type": kind,
                },
                snapshot={"objectId": published.object_id},
                followers=followers,
            )
            sources.append(source.id)
    return database, sources


def test_delete_bypasses_pending_failed_and_supersedes_only_after_success(
    tmp_path,
) -> None:
    """Terminal Delete bypasses unsent rows and supersedes them only on success."""
    database, source_ids = _content_delivery_lifecycle(tmp_path)
    with database.session() as session:
        rows = list(
            session.scalars(
                select(UserActorOutboundDelivery).order_by(
                    UserActorOutboundDelivery.source_id
                )
            )
        )
        rows[0].status = "pending"
        rows[1].status = "failed"
    claims = database.user_actor_deliveries.claim_due(
        limit=10, per_user_limit=10, stale_after_seconds=300
    )
    assert [claim.source_id for claim in claims] == [source_ids[2]]
    delete_claim = claims[0]
    database.user_actor_deliveries.record_outcome(
        delete_claim.delivery_id, delete_claim.claim_token, ok=False, error="temporary"
    )
    with database.session() as session:
        rows = list(
            session.scalars(
                select(UserActorOutboundDelivery).order_by(
                    UserActorOutboundDelivery.source_id
                )
            )
        )
        assert [row.status for row in rows] == ["pending", "failed", "failed"]

    with database.session() as session:
        delete = session.scalar(
            select(UserActorOutboundDelivery).where(
                UserActorOutboundDelivery.source_id == source_ids[2]
            )
        )
        delete.next_attempt_at = None
    claims = database.user_actor_deliveries.claim_due(
        limit=10, per_user_limit=10, stale_after_seconds=300
    )
    delete_claim = next(claim for claim in claims if claim.source_id == source_ids[2])
    database.user_actor_deliveries.record_outcome(
        delete_claim.delivery_id, delete_claim.claim_token, ok=True
    )
    with database.session() as session:
        rows = list(
            session.scalars(
                select(UserActorOutboundDelivery).order_by(
                    UserActorOutboundDelivery.source_id
                )
            )
        )
        assert [row.status for row in rows] == ["superseded", "superseded", "delivered"]


def test_delete_waits_for_sending_and_delivered_delete_blocks_stale_update(
    tmp_path,
) -> None:
    """Delete never overtakes an in-flight row; delivered Delete blocks resurrection."""
    database, source_ids = _content_delivery_lifecycle(tmp_path)
    with database.session() as session:
        rows = list(
            session.scalars(
                select(UserActorOutboundDelivery).order_by(
                    UserActorOutboundDelivery.source_id
                )
            )
        )
        rows[0].status = "delivered"
        rows[1].status = "sending"
        rows[1].claim_token = "owned"
    assert (
        database.user_actor_deliveries.claim_due(
            limit=10, per_user_limit=10, stale_after_seconds=300
        )
        == []
    )

    with database.session() as session:
        update = session.scalar(
            select(UserActorOutboundDelivery).where(
                UserActorOutboundDelivery.source_id == source_ids[1]
            )
        )
        update.status = "failed"
        update.claim_token = None
        update.next_attempt_at = None
    delete_claim = database.user_actor_deliveries.claim_due(
        limit=10, per_user_limit=10, stale_after_seconds=300
    )[0]
    database.user_actor_deliveries.record_outcome(
        delete_claim.delivery_id, delete_claim.claim_token, ok=True
    )
    with database.session() as session:
        update = session.scalar(
            select(UserActorOutboundDelivery).where(
                UserActorOutboundDelivery.source_id == source_ids[1]
            )
        )
        update.status = "pending"
    assert (
        database.user_actor_deliveries.claim_due(
            limit=10, per_user_limit=10, stale_after_seconds=300
        )
        == []
    )
    with database.session() as session:
        update = session.scalar(
            select(UserActorOutboundDelivery).where(
                UserActorOutboundDelivery.source_id == source_ids[1]
            )
        )
        assert update.status == "superseded"


@pytest.mark.asyncio
async def test_worker_cancellation_after_dispatch_keeps_same_reservation_recoverable(
    tmp_path,
) -> None:
    """Cancellation after the send boundary records ambiguous replayable state."""
    import asyncio
    from datetime import datetime, timezone
    from src.models import UserActorContentMutation

    database = Database(f"sqlite:///{tmp_path / 'cancel.db'}")
    database.create_all()
    database.migrate()
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://bridge.example/actors/users/alice",
        inbox_url="https://bridge.example/actors/users/alice/inbox",
        outbox_url="https://bridge.example/actors/users/alice/outbox",
        followers_url="https://bridge.example/actors/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    database.activitypub_objects.create_published_activity_object(
        actor_username="alice",
        actor_url=user.actor_url,
        community_actor_url="https://remote.example/c/group",
        activity_id="https://bridge.example/actors/users/alice/activities/create/posts/1",
        object_id="https://bridge.example/posts/1",
        kind="post",
        title=None,
        body_markdown="old",
        in_reply_to_object_id=None,
        discord_channel_id=10,
        discord_message_id=11,
    )

    class Gateway:
        async def update_content(self, request):
            raise asyncio.CancelledError

        async def delete_content(self, request):
            raise AssertionError

    service = UserActorDeliveryService(database, Gateway(), AllowAllPolicy())
    assert (
        database.content_mutations.record_desired_update(
            object_id="https://bridge.example/posts/1",
            title=None,
            body_markdown="new",
            edited_timestamp=datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc),
        )
        == "accepted"
    )
    database.content_mutations.reserve_next(
        object_id="https://bridge.example/posts/1", target_inbox_urls=[]
    )
    with pytest.raises(asyncio.CancelledError):
        await service.drain_mutations()
    with database.session() as session:
        mutation = session.scalar(select(UserActorContentMutation))
        assert mutation.state == "ambiguous"
        assert mutation.side_effect_classification == "send_possible"
        assert mutation.claim_token is None


def test_failed_delete_backoff_blocks_due_older_update(tmp_path) -> None:
    """A newer failed Delete remains a barrier while its retry is not due."""
    from datetime import datetime, timedelta, timezone

    database, source_ids = _content_delivery_lifecycle(tmp_path)
    with database.session() as session:
        rows = list(
            session.scalars(
                select(UserActorOutboundDelivery).order_by(
                    UserActorOutboundDelivery.source_id
                )
            )
        )
        rows[0].status = "delivered"
        rows[1].status = "failed"
        rows[1].next_attempt_at = None
        rows[2].status = "failed"
        rows[2].next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=30)

    assert (
        database.user_actor_deliveries.claim_due(
            limit=10, per_user_limit=10, stale_after_seconds=300
        )
        == []
    )
    with database.session() as session:
        update = session.scalar(
            select(UserActorOutboundDelivery).where(
                UserActorOutboundDelivery.source_id == source_ids[1]
            )
        )
        assert update.status == "failed"


def test_failed_delete_backoff_blocks_pending_create(tmp_path) -> None:
    """A pending Create is not sent while a newer Delete waits in backoff."""
    from datetime import datetime, timedelta, timezone

    database, source_ids = _content_delivery_lifecycle(tmp_path)
    with database.session() as session:
        rows = list(
            session.scalars(
                select(UserActorOutboundDelivery).order_by(
                    UserActorOutboundDelivery.source_id
                )
            )
        )
        rows[0].status = "pending"
        rows[0].next_attempt_at = None
        rows[1].status = "superseded"
        rows[2].status = "failed"
        rows[2].next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=30)

    assert (
        database.user_actor_deliveries.claim_due(
            limit=10, per_user_limit=10, stale_after_seconds=300
        )
        == []
    )
    with database.session() as session:
        create = session.scalar(
            select(UserActorOutboundDelivery).where(
                UserActorOutboundDelivery.source_id == source_ids[0]
            )
        )
        assert create.status == "pending"


@pytest.mark.asyncio
async def test_canonical_ready_reservation_commits_after_restart_without_gateway_replay(
    tmp_path,
) -> None:
    """A crash after canonical persistence commits stored JSON without another send."""
    from datetime import datetime, timezone
    from src.models import PublishedActivityObject, UserActorContentMutation

    database = Database(f"sqlite:///{tmp_path / 'canonical-ready.db'}")
    database.create_all()
    database.migrate()
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://bridge.example/actors/users/alice",
        inbox_url="https://bridge.example/actors/users/alice/inbox",
        outbox_url="https://bridge.example/actors/users/alice/outbox",
        followers_url="https://bridge.example/actors/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    database.activitypub_objects.create_published_activity_object(
        actor_username="alice",
        actor_url=user.actor_url,
        community_actor_url="https://remote.example/c/group",
        activity_id="https://bridge.example/actors/users/alice/activities/create/posts/1",
        object_id="https://bridge.example/posts/1",
        kind="post",
        title=None,
        body_markdown="old",
        in_reply_to_object_id=None,
        discord_channel_id=10,
        discord_message_id=11,
    )
    assert (
        database.content_mutations.record_desired_update(
            object_id="https://bridge.example/posts/1",
            title=None,
            body_markdown="new",
            edited_timestamp=datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc),
        )
        == "accepted"
    )
    database.content_mutations.reserve_next(
        object_id="https://bridge.example/posts/1", target_inbox_urls=[]
    )
    claim = database.content_mutations.claim_due(limit=1, stale_after_seconds=300)[0]
    assert database.content_mutations.mark_send_possible(
        claim.mutation_id, claim.claim_token
    )
    canonical = {
        "id": claim.activity_id,
        "type": "Update",
        "actor": user.actor_url,
        "object": {"id": "https://bridge.example/posts/1", "content": "new"},
    }
    assert database.content_mutations.record_canonical_result(
        claim.mutation_id, claim.claim_token, canonical
    )

    class Gateway:
        async def update_content(self, _request):
            raise AssertionError("canonical-ready recovery must not resend")

        async def delete_content(self, _request):
            raise AssertionError("delete not expected")

        async def send_user_actor_deliveries(self, **_kwargs):
            return []

    recovered = UserActorDeliveryService(database, Gateway(), AllowAllPolicy())
    await recovered.drain_mutations()

    with database.session() as session:
        published = session.scalar(select(PublishedActivityObject))
        mutation = session.scalar(select(UserActorContentMutation))
        source = session.scalar(
            select(UserActorOutboundSource).where(
                UserActorOutboundSource.source_kind == "content_update"
            )
        )
        assert published.body_markdown == "new"
        assert published.content_revision == 2
        assert mutation.state == "committed"
        assert source.activity_id == claim.activity_id


@pytest.mark.asyncio
async def test_post_commit_restart_re_evaluates_newer_desired_delete(tmp_path) -> None:
    """A crash after Update commit still reserves the already durable Delete."""
    from datetime import datetime, timezone
    from src.fedify_gateway_client import CanonicalMutationResult
    from src.models import PublishedActivityObject, UserActorContentMutation

    database = Database(f"sqlite:///{tmp_path / 'post-commit.db'}")
    database.create_all()
    database.migrate()
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://bridge.example/actors/users/alice",
        inbox_url="https://bridge.example/actors/users/alice/inbox",
        outbox_url="https://bridge.example/actors/users/alice/outbox",
        followers_url="https://bridge.example/actors/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    database.activitypub_objects.create_published_activity_object(
        actor_username="alice",
        actor_url=user.actor_url,
        community_actor_url="https://remote.example/c/group",
        activity_id="https://bridge.example/actors/users/alice/activities/create/posts/1",
        object_id="https://bridge.example/posts/1",
        kind="post",
        title=None,
        body_markdown="old",
        in_reply_to_object_id=None,
        discord_channel_id=10,
        discord_message_id=11,
    )
    assert (
        database.content_mutations.record_desired_update(
            object_id="https://bridge.example/posts/1",
            title=None,
            body_markdown="new",
            edited_timestamp=datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc),
        )
        == "accepted"
    )
    database.content_mutations.reserve_next(
        object_id="https://bridge.example/posts/1", target_inbox_urls=[]
    )
    assert (
        database.content_mutations.record_desired_delete(
            object_id="https://bridge.example/posts/1"
        )
        == "accepted"
    )

    class Gateway:
        update_calls = 0
        delete_calls = 0

        async def update_content(self, request):
            self.update_calls += 1
            return CanonicalMutationResult(
                request.activity_id,
                request.ap_object_id,
                {
                    "id": request.activity_id,
                    "type": "Update",
                    "actor": user.actor_url,
                    "object": request.ap_object_id,
                },
            )

        async def delete_content(self, request):
            self.delete_calls += 1
            return CanonicalMutationResult(
                request.activity_id,
                request.ap_object_id,
                {
                    "id": request.activity_id,
                    "type": "Delete",
                    "actor": user.actor_url,
                    "object": request.ap_object_id,
                },
            )

        async def send_user_actor_deliveries(self, **_kwargs):
            return []

    gateway = Gateway()
    first_runtime = UserActorDeliveryService(database, gateway, AllowAllPolicy())
    await first_runtime.drain_mutations(limit=1)
    # Simulate process death before the in-process follow-up drain.  A fresh
    # service discovers the durable desired Delete and completes it.
    database.content_mutations.reserve_next(
        object_id="https://bridge.example/posts/1", target_inbox_urls=[]
    )
    recovered = UserActorDeliveryService(database, gateway, AllowAllPolicy())
    await recovered.drain_mutations(limit=10)

    with database.session() as session:
        published = session.scalar(select(PublishedActivityObject))
        states = list(
            session.scalars(
                select(UserActorContentMutation.state).order_by(
                    UserActorContentMutation.id
                )
            )
        )
        assert published.deleted_at is not None
        assert published.content_revision == 3
        assert states == ["committed", "committed"]
    assert gateway.update_calls == 1
    assert gateway.delete_calls == 1


@pytest.mark.asyncio
async def test_cancellation_before_mutation_drain_leaves_reserved_work_restartable(
    tmp_path,
) -> None:
    """Shutdown before dispatch leaves the durable reservation claimable on restart."""
    from datetime import datetime, timezone
    from src.fedify_gateway_client import CanonicalMutationResult
    from src.models import UserActorContentMutation

    database = Database(f"sqlite:///{tmp_path / 'pre-dispatch-cancel.db'}")
    database.create_all()
    database.migrate()
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://bridge.example/actors/users/alice",
        inbox_url="https://bridge.example/actors/users/alice/inbox",
        outbox_url="https://bridge.example/actors/users/alice/outbox",
        followers_url="https://bridge.example/actors/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    database.activitypub_objects.create_published_activity_object(
        actor_username="alice",
        actor_url=user.actor_url,
        community_actor_url="https://remote.example/c/group",
        activity_id="https://bridge.example/actors/users/alice/activities/create/posts/1",
        object_id="https://bridge.example/posts/1",
        kind="post",
        title=None,
        body_markdown="old",
        in_reply_to_object_id=None,
        discord_channel_id=10,
        discord_message_id=11,
    )
    assert (
        database.content_mutations.record_desired_update(
            object_id="https://bridge.example/posts/1",
            title=None,
            body_markdown="new",
            edited_timestamp=datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc),
        )
        == "accepted"
    )
    database.content_mutations.reserve_next(
        object_id="https://bridge.example/posts/1", target_inbox_urls=[]
    )

    class Gateway:
        calls = 0

        async def update_content(self, request):
            self.calls += 1
            return CanonicalMutationResult(
                request.activity_id,
                request.ap_object_id,
                {
                    "id": request.activity_id,
                    "type": "Update",
                    "actor": user.actor_url,
                    "object": request.ap_object_id,
                },
            )

        async def delete_content(self, _request):
            raise AssertionError

        async def send_user_actor_deliveries(self, **_kwargs):
            return []

    with database.session() as session:
        mutation = session.scalar(select(UserActorContentMutation))
        assert mutation.state == "reserved"
        assert mutation.claim_token is None

    gateway = Gateway()
    await UserActorDeliveryService(
        database, gateway, AllowAllPolicy()
    ).drain_mutations()
    assert gateway.calls == 1
