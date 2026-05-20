"""Behavior scenarios for local-community remote follower federation fanout."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.activitypub_models import ActivityPubEvent
from src.fedify_gateway_client import SendLocalCommunityRelayResult, SendLocalCommunityRelayOutcome
from src.discord_publish_service import ContentPublishService
from src.local_communities.runtime import LocalCommunityRuntime
from src.local_communities.service import LocalCommunityService
from support.db import build_database
from support.discord import build_bot, build_forum_channel_tuple_result, build_send_thread


def _runtime(tmp_path: Path) -> tuple[object, LocalCommunityRuntime]:
    """Build one local-community runtime with a fake gateway boundary."""
    database = build_database(tmp_path, "local-community-remote-fanout.db")
    gateway = AsyncMock()
    gateway.send_local_community_relay.return_value = SendLocalCommunityRelayResult(
        outcomes=[]
    )
    publish_service = ContentPublishService(
        database=database,
        fedify_gateway=gateway,
        bridge_prefix="[bridge]",
    )
    runtime = LocalCommunityRuntime(
        database=database,
        fedify_gateway=gateway,
        content_publish_service=publish_service,
        bridge_prefix="[bridge]",
    )
    return database, runtime


def _local_community(database: object) -> object:
    """Seed one local community and return its row."""
    LocalCommunityService(
        database=database,
        base_url="https://bridge.example",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=100,
        slug="hackers",
        name="Hackers",
        description="A local hackerspace forum.",
    )
    return database.get_local_community_by_slug("hackers")


def _add_followers(database: object, local_community: object) -> None:
    """Create one origin follower and two relay targets."""
    for name in ["bob", "alice", "carol"]:
        database.create_local_community_follower(
            local_community_id=local_community.id,
            remote_actor_id=f"https://lemmy.example/u/{name}",
            remote_inbox_url=f"https://lemmy.example/u/{name}/inbox",
            follow_activity_id=f"https://lemmy.example/activities/follow/{name}",
        )


def _post_event(*, suffix: str = "1") -> ActivityPubEvent:
    """Build one inbound post event carrying the original source Create."""
    source = {
        "type": "Create",
        "id": f"https://lemmy.example/activities/create/post/{suffix}",
        "actor": "https://lemmy.example/u/bob",
        "object": {
            "type": "Page",
            "id": f"https://lemmy.example/post/{suffix}",
            "attributedTo": "https://lemmy.example/u/bob",
            "name": "Remote topic",
            "content": "<p>hello</p>",
        },
    }
    return ActivityPubEvent.model_validate(
        {
            "event_type": "post.created",
            "delivery_id": source["id"],
            "source_activity_json": source,
            "source_activity_id": source["id"],
            "source_announce_id": "https://lemmy.example/activities/announce/1",
            "occurred_at": "2026-05-19T10:00:00Z",
            "community_actor_id": "https://bridge.example/communities/hackers",
            "actor_id": "https://lemmy.example/u/bob",
            "object": {
                "ap_id": f"https://lemmy.example/post/{suffix}",
                "kind": "post",
                "lemmy_id": 1,
                "post_ap_id": None,
                "post_lemmy_id": None,
                "parent_ap_id": None,
                "title": "Remote topic",
                "body_markdown": "hello",
                "url": f"https://lemmy.example/post/{suffix}",
                "published_at": "2026-05-19T10:00:00Z",
                "author_name": "bob",
            },
        }
    )


@pytest.mark.asyncio
async def test_accepted_remote_post_relays_to_other_followers_only(tmp_path: Path) -> None:
    """A remote follower post should mirror to Discord and fan out to other followers."""
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    _add_followers(database, local_community)
    forum_channel = build_forum_channel_tuple_result(channel_id=100, thread_id=200, starter_message_id=300)
    runtime.bot = build_bot(forum_channels={100: forum_channel})

    async def gateway_result(*, signing_actor_url: str, deliveries: list[object]) -> SendLocalCommunityRelayResult:
        """Mark every requested target as delivered for DB-state assertions."""
        return SendLocalCommunityRelayResult(
            outcomes=[
                SendLocalCommunityRelayOutcome(
                    delivery_id=delivery.delivery_id,
                    ok=True,
                    target_remote_actor_id=delivery.target_remote_actor_id,
                    activity_id=delivery.activity_json["id"],
                )
                for delivery in deliveries
            ]
        )

    runtime.fedify_gateway.send_local_community_relay.side_effect = gateway_result

    result = await runtime.handle_inbound_post(_post_event(), SimpleNamespace())

    assert result.status == "processed"
    runtime.fedify_gateway.send_local_community_relay.assert_awaited_once()
    request = runtime.fedify_gateway.send_local_community_relay.await_args.kwargs
    assert request["signing_actor_url"] == local_community.actor_url
    assert sorted(delivery.target_remote_actor_id for delivery in request["deliveries"]) == [
        "https://lemmy.example/u/alice",
        "https://lemmy.example/u/carol",
    ]
    assert all(delivery.activity_json["type"] == "Announce" for delivery in request["deliveries"])
    assert all(delivery.activity_json["object"]["actor"] == "https://lemmy.example/u/bob" for delivery in request["deliveries"])
    delivered = database.list_delivered_local_community_create_relay_targets(
        local_community_id=local_community.id,
        source_object_ap_id="https://lemmy.example/post/1",
    )
    assert len(delivered) == 2


@pytest.mark.asyncio
async def test_duplicate_post_recovers_missing_relay_rows_without_discord_duplicate(tmp_path: Path) -> None:
    """A mapping-only crash should be repaired on duplicate delivery."""
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    _add_followers(database, local_community)
    database.create_local_community_thread(
        local_community_id=local_community.id,
        discord_thread_id=200,
        discord_starter_message_id=300,
        ap_activity_id="https://lemmy.example/activities/create/post/1",
        ap_object_id="https://lemmy.example/post/1",
        direction="ap_to_discord",
        origin_kind="remote_follower",
    )

    async def gateway_result(*, signing_actor_url: str, deliveries: list[object]) -> SendLocalCommunityRelayResult:
        """Return success so recovered rows become durable delivered state."""
        return SendLocalCommunityRelayResult(
            outcomes=[
                SendLocalCommunityRelayOutcome(
                    delivery_id=delivery.delivery_id,
                    ok=True,
                    target_remote_actor_id=delivery.target_remote_actor_id,
                    activity_id=delivery.activity_json["id"],
                )
                for delivery in deliveries
            ]
        )

    runtime.fedify_gateway.send_local_community_relay.side_effect = gateway_result

    result = await runtime.handle_inbound_post(_post_event(), SimpleNamespace())

    assert result.status == "skipped"
    runtime.fedify_gateway.send_local_community_relay.assert_awaited_once()
    delivered = database.list_delivered_local_community_create_relay_targets(
        local_community_id=local_community.id,
        source_object_ap_id="https://lemmy.example/post/1",
    )
    assert len(delivered) == 2


def _post_update_event() -> ActivityPubEvent:
    """Build one inbound post update carrying the original source Update."""
    source = {
        "type": "Update",
        "id": "https://lemmy.example/activities/update/post/1",
        "actor": "https://lemmy.example/u/bob",
        "object": {
            "type": "Page",
            "id": "https://lemmy.example/post/1",
            "attributedTo": "https://lemmy.example/u/bob",
            "name": "Remote topic edited",
            "content": "<p>edited</p>",
        },
    }
    return ActivityPubEvent.model_validate(
        {
            "event_type": "post.updated",
            "delivery_id": source["id"],
            "source_activity_json": source,
            "source_activity_id": source["id"],
            "source_announce_id": "https://lemmy.example/activities/announce/update/1",
            "occurred_at": "2026-05-19T10:10:00Z",
            "community_actor_id": "https://bridge.example/communities/hackers",
            "actor_id": "https://lemmy.example/u/bob",
            "object": {
                "ap_id": "https://lemmy.example/post/1",
                "kind": "post",
                "lemmy_id": 1,
                "post_ap_id": None,
                "post_lemmy_id": None,
                "parent_ap_id": None,
                "title": "Remote topic edited",
                "body_markdown": "edited",
                "url": "https://lemmy.example/post/1",
                "published_at": "2026-05-19T10:10:00Z",
                "author_name": "bob",
            },
        }
    )


@pytest.mark.asyncio
async def test_inbound_post_update_relays_only_to_delivered_create_targets(tmp_path: Path) -> None:
    """A remote update should target only followers with delivered create relay rows."""
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    _add_followers(database, local_community)
    forum_channel = build_forum_channel_tuple_result(channel_id=100, thread_id=200, starter_message_id=300)
    starter_message = SimpleNamespace(edit=AsyncMock())
    runtime.bot = build_bot(
        forum_channels={100: forum_channel},
        threads={200: SimpleNamespace(fetch_message=AsyncMock(return_value=starter_message))},
    )

    async def gateway_result(*, signing_actor_url: str, deliveries: list[object]) -> SendLocalCommunityRelayResult:
        """Deliver every relay request so update continuity has concrete rows."""
        return SendLocalCommunityRelayResult(
            outcomes=[
                SendLocalCommunityRelayOutcome(
                    delivery_id=delivery.delivery_id,
                    ok=True,
                    target_remote_actor_id=delivery.target_remote_actor_id,
                    activity_id=delivery.activity_json["id"],
                )
                for delivery in deliveries
            ]
        )

    runtime.fedify_gateway.send_local_community_relay.side_effect = gateway_result
    await runtime.handle_inbound_post(_post_event(), SimpleNamespace())
    runtime.fedify_gateway.send_local_community_relay.reset_mock(side_effect=True)
    runtime.fedify_gateway.send_local_community_relay.side_effect = gateway_result

    result = await runtime.handle_inbound_post_update(_post_update_event(), SimpleNamespace(bot=runtime.bot))

    assert result.status == "processed"
    request = runtime.fedify_gateway.send_local_community_relay.await_args.kwargs
    assert sorted(delivery.target_remote_actor_id for delivery in request["deliveries"]) == [
        "https://lemmy.example/u/alice",
        "https://lemmy.example/u/carol",
    ]
    assert all(delivery.activity_json["object"]["type"] == "Update" for delivery in request["deliveries"])
