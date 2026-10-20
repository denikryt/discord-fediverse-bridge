"""typed actor identity migration local-community Announce identity contract."""

from __future__ import annotations

from types import SimpleNamespace

from src.local_communities.activitypub_renderers import render_local_community_relay_activity


def test_local_community_relay_announce_uses_canonical_post_grammar() -> None:
    activity = render_local_community_relay_activity(
        source_activity_json={
            "id": "https://remote.example/create/1",
            "type": "Create",
            "actor": "https://remote.example/u/bob",
            "object": {"id": "https://remote.example/post/1", "type": "Page"},
        },
        normalized_event=SimpleNamespace(event_type="post.created", object=SimpleNamespace(kind="post")),
        community_actor_url="https://bridge.example/actors/communities/python",
        community_slug="python",
        delivery_profile="threadiverse_group",
        object_kind="post",
        relay_activity_id="https://bridge.example/actors/communities/python/activities/announce/posts/reserved",
    )

    assert activity["id"].startswith(
        "https://bridge.example/actors/communities/python/activities/announce/posts/"
    )
    assert "/activities/announce/20" not in activity["id"]


def test_local_community_relay_announce_uses_canonical_comment_grammar() -> None:
    event_object = SimpleNamespace(
        kind="comment",
        ap_id="https://remote.example/comment/1",
        parent_ap_id="https://remote.example/post/1",
        post_ap_id="https://remote.example/post/1",
        body_markdown="hello",
        published_at="2026-06-21T12:00:00Z",
        url="https://remote.example/comment/1",
    )
    activity = render_local_community_relay_activity(
        source_activity_json={
            "id": "https://remote.example/create/1",
            "type": "Create",
            "actor": "https://remote.example/u/bob",
            "to": "as:Public",
            "object": {
                "id": "https://remote.example/comment/1",
                "type": "Note",
                "to": "as:Public",
                "content": "hello",
            },
        },
        normalized_event=SimpleNamespace(
            event_type="comment.created",
            object=event_object,
            actor_id="https://remote.example/u/bob",
            delivery_id="https://remote.example/create/1",
        ),
        community_actor_url="https://bridge.example/actors/communities/python",
        community_slug="python",
        delivery_profile="threadiverse_group",
        object_kind="comment",
        relay_activity_id="https://bridge.example/actors/communities/python/activities/announce/comments/reserved",
    )

    assert activity["id"].startswith(
        "https://bridge.example/actors/communities/python/activities/announce/comments/"
    )
