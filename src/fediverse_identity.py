"""Remote Fediverse identity helpers for moderator-facing bridge features.

This module intentionally performs only local string validation and parsing. It
must not do WebFinger, actor fetches, or any other network call because inbound
ActivityPub hot paths use these helpers before domain side effects happen.
"""

from __future__ import annotations

from urllib.parse import urlparse


class InvalidRemoteActorHandle(ValueError):
    """Signal that a moderator supplied a handle outside the v1 command format."""


def normalize_remote_actor_handle(value: str) -> str:
    """Normalize the `user@example.com` handle accepted by moderation commands.

    The v1 command accepts the same visible handle shape that the bridge renders
    in Discord. It rejects ActivityPub actor URLs and `acct:` URIs so operators
    get one stable input contract while identity-resolution work remains future
    work.
    """
    raw = value.strip()
    lowered = raw.lower()
    if raw.startswith("@") or lowered.startswith("acct:"):
        raise InvalidRemoteActorHandle("remote actor handle must be user@example.com")
    if lowered.startswith("http://") or lowered.startswith("https://"):
        raise InvalidRemoteActorHandle("remote actor handle must not be an actor URL")
    if raw.count("@") != 1:
        raise InvalidRemoteActorHandle("remote actor handle must contain exactly one @")

    username, domain = raw.split("@", 1)
    if not username or not domain:
        raise InvalidRemoteActorHandle("remote actor handle must include username and domain")

    # Domain names are case-insensitive. Preserve username casing because the
    # current Discord-facing formatting shows preferred usernames as supplied by
    # remote metadata rather than forcing every display label to lowercase.
    return f"{username}@{domain.lower()}"


def extract_remote_actor_handle_from_actor_url(actor_url: str) -> str | None:
    """Return a best-effort `user@example.com` handle from a common actor URL.

    This is deliberately not a full Fediverse resolver. It supports the common
    `/u/name` and `/users/name` shapes, plus any URL whose final non-empty path
    segment is the actor username. Unknown or malformed inputs return `None` so
    callers can continue existing fallback behavior without inventing a global
    ban match.
    """
    parsed = urlparse(actor_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None

    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        return None

    username = segments[-1]
    if not username:
        return None

    # urlparse lowercases hostname, matching DNS semantics and command-domain
    # normalization. The final path segment remains case-preserving.
    return f"{username}@{parsed.hostname.lower()}"
