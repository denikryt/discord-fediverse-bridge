"""Canonical local ActivityPub actor, object, and activity URL builders.

This module defines the future typed identity grammar without changing any
current public route, WebFinger, database, or delivery behavior. It is pure by
construction: callers provide the public ActivityPub origin and local identity
parts, and the module returns deterministic absolute URLs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote, unquote, urlsplit

# These patterns intentionally duplicate the existing registration and
# local-community service contracts. Importing those modules here would pull in
# database-facing service dependencies, which would make this pure helper a
# wider runtime boundary than Stage 2 needs.
_USERNAME_PATTERN = re.compile(r"^[a-z0-9_-]+$")
_COMMUNITY_SLUG_PATTERN = re.compile(r"^[a-z0-9_-]+$")


class LocalActorKind(StrEnum):
    """Enumerate the local actor categories supported by the typed namespace."""

    SERVICE = "service"
    USER = "user"
    COMMUNITY = "community"


@dataclass(frozen=True, slots=True)
class ActorAddress:
    """Describe one canonical local actor URL family.

    The dataclass carries all actor-owned collection and key URLs together so
    later cutover code cannot accidentally mix a typed actor id with an old
    inbox, outbox, followers, or public-key owner URL.
    """

    kind: LocalActorKind
    identifier: str
    actor_url: str
    inbox_url: str
    outbox_url: str
    followers_url: str
    public_key_url: str


_KIND_TO_SEGMENT: dict[LocalActorKind, str] = {
    LocalActorKind.SERVICE: "services",
    LocalActorKind.USER: "users",
    LocalActorKind.COMMUNITY: "communities",
}
_SEGMENT_TO_KIND: dict[str, LocalActorKind] = {
    segment: kind for kind, segment in _KIND_TO_SEGMENT.items()
}


def _normalize_origin(origin: str) -> str:
    """Return an origin-only URL string or raise when a prefix URL is supplied."""
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("origin must be an absolute http(s) origin")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("origin must not contain userinfo")
    if parsed.path not in {"", "/"}:
        raise ValueError("origin must not contain a non-root path")
    if parsed.query or parsed.fragment:
        raise ValueError("origin must not contain query or fragment components")
    # Hostname is lower-cased by urlsplit.hostname. Rebuilding the netloc avoids
    # preserving userinfo and gives parser comparisons one canonical spelling.
    host = parsed.hostname
    if host is None:
        raise ValueError("origin must include a host")
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{host}{port}"


def _coerce_kind(kind: LocalActorKind | str) -> LocalActorKind:
    """Coerce caller input into a known local actor kind."""
    try:
        return LocalActorKind(kind)
    except ValueError as exc:
        raise ValueError(f"unknown local actor kind: {kind!r}") from exc


def _validate_user_identifier(identifier: str) -> None:
    """Validate one local user identifier using the registration username policy."""
    if not identifier or not _USERNAME_PATTERN.fullmatch(identifier):
        raise ValueError("user identifier must match existing username policy")


def _validate_community_identifier(identifier: str) -> None:
    """Validate one local community identifier using the slug policy."""
    if not identifier or not _COMMUNITY_SLUG_PATTERN.fullmatch(identifier):
        raise ValueError("community identifier must match existing slug policy")


def _validate_service_identifier(identifier: str) -> None:
    """Validate the service actor identifier as one non-empty URL path segment."""
    if not identifier or any(separator in identifier for separator in ("/", "?", "#")):
        raise ValueError("service identifier must be one non-empty path segment")


def _validate_identifier(kind: LocalActorKind, identifier: str) -> None:
    """Apply the current per-kind local actor identifier contract."""
    if kind is LocalActorKind.USER:
        _validate_user_identifier(identifier)
    elif kind is LocalActorKind.COMMUNITY:
        _validate_community_identifier(identifier)
    else:
        _validate_service_identifier(identifier)


def _segment(value: str | int) -> str:
    """Encode one non-empty path segment for deterministic URL construction."""
    text = str(value)
    if text == "":
        raise ValueError("path segment must not be empty")
    return quote(text, safe="")


def _actor_root(origin: str, kind: LocalActorKind, identifier: str) -> str:
    """Build the encoded actor root shared by actor/object/activity helpers."""
    normalized_origin = _normalize_origin(origin)
    _validate_identifier(kind, identifier)
    return f"{normalized_origin}/actors/{_KIND_TO_SEGMENT[kind]}/{_segment(identifier)}"


def build_actor_address(
    *, origin: str, kind: LocalActorKind | str, identifier: str
) -> ActorAddress:
    """Build the canonical typed address family for one local actor."""
    actor_kind = _coerce_kind(kind)
    actor_url = _actor_root(origin, actor_kind, identifier)
    return ActorAddress(
        kind=actor_kind,
        identifier=identifier,
        actor_url=actor_url,
        inbox_url=f"{actor_url}/inbox",
        outbox_url=f"{actor_url}/outbox",
        followers_url=f"{actor_url}/followers",
        public_key_url=f"{actor_url}#main-key",
    )


def parse_local_actor_url(*, origin: str, actor_url: str) -> ActorAddress | None:
    """Classify a same-origin typed actor URL or return ``None`` when invalid."""
    try:
        normalized_origin = _normalize_origin(origin)
    except ValueError:
        return None
    try:
        parsed = urlsplit(actor_url)
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            return None
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        host = parsed.hostname
        if host is None:
            return None
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return None
    if f"{parsed.scheme}://{host}{port}" != normalized_origin:
        return None

    parts = parsed.path.split("/")
    if len(parts) != 4 or parts[0] != "" or parts[1] != "actors":
        return None
    kind = _SEGMENT_TO_KIND.get(parts[2])
    if kind is None or not parts[3]:
        return None
    identifier = unquote(parts[3])
    # Encoded slashes would make one URL path segment mean multiple logical
    # identifiers to different routers, so classify them as malformed.
    if "/" in identifier:
        return None
    try:
        return build_actor_address(
            origin=normalized_origin, kind=kind, identifier=identifier
        )
    except ValueError:
        return None


def _user_root(origin: str, username: str) -> str:
    """Return the canonical typed root for one local user actor."""
    return _actor_root(origin, LocalActorKind.USER, username)


def _community_root(origin: str, slug: str) -> str:
    """Return the canonical typed root for one local community actor."""
    return _actor_root(origin, LocalActorKind.COMMUNITY, slug)


def _service_root(origin: str, identifier: str) -> str:
    """Return the canonical typed root for one local service actor."""
    return _actor_root(origin, LocalActorKind.SERVICE, identifier)


def build_user_post_object_id(*, origin: str, username: str, object_id: str) -> str:
    """Build a typed local user post object id."""
    return f"{_user_root(origin, username)}/objects/posts/{_segment(object_id)}"


def build_user_comment_object_id(*, origin: str, username: str, object_id: str) -> str:
    """Build a typed local user comment object id."""
    return f"{_user_root(origin, username)}/objects/comments/{_segment(object_id)}"


def build_user_create_post_activity_id(
    *, origin: str, username: str, activity_id: str
) -> str:
    """Build a typed local user Create(Post) activity id."""
    return f"{_user_root(origin, username)}/activities/create/posts/{_segment(activity_id)}"


def build_user_create_comment_activity_id(
    *, origin: str, username: str, activity_id: str
) -> str:
    """Build a typed local user Create(Comment) activity id."""
    return f"{_user_root(origin, username)}/activities/create/comments/{_segment(activity_id)}"


def build_user_update_post_activity_id(
    *, origin: str, username: str, activity_id: str
) -> str:
    """Build a typed local user Update(Post) activity id."""
    return f"{_user_root(origin, username)}/activities/update/posts/{_segment(activity_id)}"


def build_user_update_comment_activity_id(
    *, origin: str, username: str, activity_id: str
) -> str:
    """Build a typed local user Update(Comment) activity id."""
    return f"{_user_root(origin, username)}/activities/update/comments/{_segment(activity_id)}"


def build_user_delete_post_activity_id(
    *, origin: str, username: str, activity_id: str
) -> str:
    """Build a typed local user Delete(Post) activity id."""
    return f"{_user_root(origin, username)}/activities/delete/posts/{_segment(activity_id)}"


def build_user_delete_comment_activity_id(
    *, origin: str, username: str, activity_id: str
) -> str:
    """Build a typed local user Delete(Comment) activity id."""
    return f"{_user_root(origin, username)}/activities/delete/comments/{_segment(activity_id)}"


def build_user_accept_follow_activity_id(
    *, origin: str, username: str, activity_id: str
) -> str:
    """Build a typed local user Accept(Follow) activity id."""
    return f"{_user_root(origin, username)}/activities/accept/follows/{_segment(activity_id)}"


def build_user_profile_update_activity_id(
    *, origin: str, username: str, profile_version_or_activity_id: str | int
) -> str:
    """Build a typed local user profile Update activity id."""
    return f"{_user_root(origin, username)}/activities/update/profile/{_segment(profile_version_or_activity_id)}"


def build_community_post_object_id(*, origin: str, slug: str, object_id: str) -> str:
    """Build a typed local community post object id."""
    return f"{_community_root(origin, slug)}/objects/posts/{_segment(object_id)}"


def build_community_comment_object_id(
    *, origin: str, slug: str, object_id: str
) -> str:
    """Build a typed local community comment object id."""
    return f"{_community_root(origin, slug)}/objects/comments/{_segment(object_id)}"


def build_community_create_post_activity_id(
    *, origin: str, slug: str, activity_id: str
) -> str:
    """Build a typed local community Create(Post) activity id."""
    return f"{_community_root(origin, slug)}/activities/create/posts/{_segment(activity_id)}"


def build_community_create_comment_activity_id(
    *, origin: str, slug: str, activity_id: str
) -> str:
    """Build a typed local community Create(Comment) activity id."""
    return f"{_community_root(origin, slug)}/activities/create/comments/{_segment(activity_id)}"


def build_community_announce_post_activity_id(
    *, origin: str, slug: str, activity_id: str
) -> str:
    """Build a typed local community Announce(Post) activity id."""
    return f"{_community_root(origin, slug)}/activities/announce/posts/{_segment(activity_id)}"


def build_community_announce_comment_activity_id(
    *, origin: str, slug: str, activity_id: str
) -> str:
    """Build a typed local community Announce(Comment) activity id."""
    return f"{_community_root(origin, slug)}/activities/announce/comments/{_segment(activity_id)}"


def build_community_update_post_activity_id(
    *, origin: str, slug: str, activity_id: str
) -> str:
    """Build a typed local community Update(Post) activity id."""
    return f"{_community_root(origin, slug)}/activities/update/posts/{_segment(activity_id)}"


def build_community_update_comment_activity_id(
    *, origin: str, slug: str, activity_id: str
) -> str:
    """Build a typed local community Update(Comment) activity id."""
    return f"{_community_root(origin, slug)}/activities/update/comments/{_segment(activity_id)}"


def build_community_delete_post_activity_id(
    *, origin: str, slug: str, activity_id: str
) -> str:
    """Build a typed local community Delete(Post) activity id."""
    return f"{_community_root(origin, slug)}/activities/delete/posts/{_segment(activity_id)}"


def build_community_delete_comment_activity_id(
    *, origin: str, slug: str, activity_id: str
) -> str:
    """Build a typed local community Delete(Comment) activity id."""
    return f"{_community_root(origin, slug)}/activities/delete/comments/{_segment(activity_id)}"


def build_community_accept_follow_activity_id(
    *, origin: str, slug: str, activity_id: str
) -> str:
    """Build a typed local community Accept(Follow) activity id."""
    return f"{_community_root(origin, slug)}/activities/accept/follows/{_segment(activity_id)}"


def build_service_follow_activity_id(
    *, origin: str, identifier: str, activity_id: str
) -> str:
    """Build a typed local service actor Follow activity id."""
    return f"{_service_root(origin, identifier)}/activities/follow/{_segment(activity_id)}"


def build_service_undo_follow_activity_id(
    *, origin: str, identifier: str, activity_id: str
) -> str:
    """Build a typed local service actor Undo(Follow) activity id."""
    return f"{_service_root(origin, identifier)}/activities/undo/follows/{_segment(activity_id)}"
