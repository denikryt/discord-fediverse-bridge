"""Tests for canonical local ActivityPub identity URL helpers."""

from __future__ import annotations

import pytest

from src.activitypub_identity_urls import (
    LocalActorKind,
    build_actor_address,
    build_community_accept_follow_activity_id,
    build_community_announce_comment_activity_id,
    build_community_announce_post_activity_id,
    build_community_comment_object_id,
    build_community_create_comment_activity_id,
    build_community_create_post_activity_id,
    build_community_delete_comment_activity_id,
    build_community_delete_post_activity_id,
    build_community_post_object_id,
    build_community_update_comment_activity_id,
    build_community_update_post_activity_id,
    build_service_follow_activity_id,
    build_service_undo_follow_activity_id,
    build_user_accept_follow_activity_id,
    build_user_comment_object_id,
    build_user_create_comment_activity_id,
    build_user_create_post_activity_id,
    build_user_delete_comment_activity_id,
    build_user_delete_post_activity_id,
    build_user_post_object_id,
    build_user_profile_update_activity_id,
    build_user_update_comment_activity_id,
    build_user_update_post_activity_id,
    parse_local_actor_url,
)


def test_builds_typed_actor_address_fields() -> None:
    """Service, user, and community actors share one typed URL grammar."""
    user = build_actor_address(
        origin="https://bridge.example", kind="user", identifier="alice"
    )
    community = build_actor_address(
        origin="https://bridge.example/", kind=LocalActorKind.COMMUNITY, identifier="python"
    )
    service = build_actor_address(
        origin="https://bridge.example", kind="service", identifier="bridge"
    )

    assert user.actor_url == "https://bridge.example/actors/users/alice"
    assert user.inbox_url == "https://bridge.example/actors/users/alice/inbox"
    assert user.outbox_url == "https://bridge.example/actors/users/alice/outbox"
    assert user.followers_url == "https://bridge.example/actors/users/alice/followers"
    assert user.public_key_url == "https://bridge.example/actors/users/alice#main-key"
    assert community.followers_url == (
        "https://bridge.example/actors/communities/python/followers"
    )
    assert service.public_key_url == (
        "https://bridge.example/actors/services/bridge#main-key"
    )
    assert build_actor_address(
        origin="https://bridge.example:8443/", kind="user", identifier="alice"
    ).actor_url == "https://bridge.example:8443/actors/users/alice"


@pytest.mark.parametrize(
    "origin",
    [
        "https://bridge.example/base",
        "https://bridge.example/app/",
        "https://bridge.example?x=1",
        "https://bridge.example#x",
        "https://user:pass@bridge.example",
        "/bridge",
    ],
)
def test_builder_rejects_non_origin_values(origin: str) -> None:
    """The identity root is an ActivityPub origin, not a reverse-proxy prefix."""
    with pytest.raises(ValueError):
        build_actor_address(origin=origin, kind="user", identifier="alice")


def test_parse_preserves_actor_kind_for_collision_resistance() -> None:
    """The parser distinguishes same identifier text under different actor kinds."""
    user = parse_local_actor_url(
        origin="https://bridge.example",
        actor_url="https://bridge.example/actors/users/python",
    )
    community = parse_local_actor_url(
        origin="https://bridge.example",
        actor_url="https://bridge.example/actors/communities/python",
    )

    assert user is not None
    assert community is not None
    assert user.kind is LocalActorKind.USER
    assert user.identifier == "python"
    assert community.kind is LocalActorKind.COMMUNITY
    assert community.identifier == "python"
    assert user.actor_url != community.actor_url


@pytest.mark.parametrize(
    "actor_url",
    [
        "https://bridge.example/actors/alice",
        "https://bridge.example/communities/python",
        "https://bridge.example/users/alice",
        "https://bridge.example/c/python",
        "https://bridge.example/actors/user/alice",
        "https://bridge.example/actors/users/alice/inbox",
        "https://bridge.example/actors/services/",
        "https://bridge.example/actors/users/alice?x=1",
        "https://bridge.example/actors/users/alice#main-key",
        "https://bridge.example/actors/users/alice%2Fbob",
        "https://user:pass@bridge.example/actors/users/alice",
        "https://remote.example/actors/users/alice",
        "https://bridge.example:bad/actors/users/alice",
    ],
)
def test_parse_rejects_old_remote_and_malformed_actor_urls(actor_url: str) -> None:
    """Only same-origin canonical actor URLs classify as local actor identities."""
    assert parse_local_actor_url(origin="https://bridge.example", actor_url=actor_url) is None


def test_builders_encode_non_actor_terminal_segments() -> None:
    """Terminal object/activity ids are path segments and are percent-encoded."""
    assert build_user_post_object_id(
        origin="https://bridge.example", username="alice", object_id="id with space"
    ) == "https://bridge.example/actors/users/alice/objects/posts/id%20with%20space"


@pytest.mark.parametrize(
    ("builder", "kwargs", "expected"),
    [
        (
            build_user_post_object_id,
            {"username": "alice", "object_id": "post1"},
            "https://bridge.example/actors/users/alice/objects/posts/post1",
        ),
        (
            build_user_comment_object_id,
            {"username": "alice", "object_id": "comment1"},
            "https://bridge.example/actors/users/alice/objects/comments/comment1",
        ),
        (
            build_user_create_post_activity_id,
            {"username": "alice", "activity_id": "a1"},
            "https://bridge.example/actors/users/alice/activities/create/posts/a1",
        ),
        (
            build_user_create_comment_activity_id,
            {"username": "alice", "activity_id": "a2"},
            "https://bridge.example/actors/users/alice/activities/create/comments/a2",
        ),
        (
            build_user_update_post_activity_id,
            {"username": "alice", "activity_id": "a3"},
            "https://bridge.example/actors/users/alice/activities/update/posts/a3",
        ),
        (
            build_user_update_comment_activity_id,
            {"username": "alice", "activity_id": "a4"},
            "https://bridge.example/actors/users/alice/activities/update/comments/a4",
        ),
        (
            build_user_delete_post_activity_id,
            {"username": "alice", "activity_id": "a5"},
            "https://bridge.example/actors/users/alice/activities/delete/posts/a5",
        ),
        (
            build_user_delete_comment_activity_id,
            {"username": "alice", "activity_id": "a6"},
            "https://bridge.example/actors/users/alice/activities/delete/comments/a6",
        ),
        (
            build_user_accept_follow_activity_id,
            {"username": "alice", "activity_id": "f1"},
            "https://bridge.example/actors/users/alice/activities/accept/follows/f1",
        ),
        (
            build_user_profile_update_activity_id,
            {"username": "alice", "profile_version_or_activity_id": 7},
            "https://bridge.example/actors/users/alice/activities/update/profile/7",
        ),
        (
            build_community_post_object_id,
            {"slug": "python", "object_id": "post1"},
            "https://bridge.example/actors/communities/python/objects/posts/post1",
        ),
        (
            build_community_comment_object_id,
            {"slug": "python", "object_id": "comment1"},
            "https://bridge.example/actors/communities/python/objects/comments/comment1",
        ),
        (
            build_community_create_post_activity_id,
            {"slug": "python", "activity_id": "a1"},
            "https://bridge.example/actors/communities/python/activities/create/posts/a1",
        ),
        (
            build_community_create_comment_activity_id,
            {"slug": "python", "activity_id": "a2"},
            "https://bridge.example/actors/communities/python/activities/create/comments/a2",
        ),
        (
            build_community_announce_post_activity_id,
            {"slug": "python", "activity_id": "a3"},
            "https://bridge.example/actors/communities/python/activities/announce/posts/a3",
        ),
        (
            build_community_announce_comment_activity_id,
            {"slug": "python", "activity_id": "a4"},
            "https://bridge.example/actors/communities/python/activities/announce/comments/a4",
        ),
        (
            build_community_update_post_activity_id,
            {"slug": "python", "activity_id": "a5"},
            "https://bridge.example/actors/communities/python/activities/update/posts/a5",
        ),
        (
            build_community_update_comment_activity_id,
            {"slug": "python", "activity_id": "a6"},
            "https://bridge.example/actors/communities/python/activities/update/comments/a6",
        ),
        (
            build_community_delete_post_activity_id,
            {"slug": "python", "activity_id": "a7"},
            "https://bridge.example/actors/communities/python/activities/delete/posts/a7",
        ),
        (
            build_community_delete_comment_activity_id,
            {"slug": "python", "activity_id": "a8"},
            "https://bridge.example/actors/communities/python/activities/delete/comments/a8",
        ),
        (
            build_community_accept_follow_activity_id,
            {"slug": "python", "activity_id": "f1"},
            "https://bridge.example/actors/communities/python/activities/accept/follows/f1",
        ),
        (
            build_service_follow_activity_id,
            {"identifier": "bridge", "activity_id": "f1"},
            "https://bridge.example/actors/services/bridge/activities/follow/f1",
        ),
        (
            build_service_undo_follow_activity_id,
            {"identifier": "bridge", "activity_id": "u1"},
            "https://bridge.example/actors/services/bridge/activities/undo/follows/u1",
        ),
    ],
)
def test_builds_every_canonical_object_and_activity_family(
    builder: object, kwargs: dict[str, object], expected: str
) -> None:
    """Every umbrella-defined locally owned object/activity family has a helper."""
    assert builder(origin="https://bridge.example", **kwargs) == expected  # type: ignore[operator]


@pytest.mark.parametrize(
    ("kind", "identifier"),
    [
        ("user", "Alice"),
        ("community", "bad/slug"),
        ("service", "bad/bridge"),
        ("service", ""),
    ],
)
def test_build_actor_address_rejects_invalid_local_identifiers(
    kind: str, identifier: str
) -> None:
    """Builder validation mirrors current local actor identifier contracts."""
    with pytest.raises(ValueError):
        build_actor_address(
            origin="https://bridge.example", kind=kind, identifier=identifier
        )
