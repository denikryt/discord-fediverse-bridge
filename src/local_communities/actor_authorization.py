"""Authorize signed remote Person content through one accepted Bridge Service.

Fedify authenticates the outer activity before issuing the internal event.
Only that outer sender can exercise a subscriber's rights; an inner Create
inside an Announce is not separately signed by the claimed Person.  For a
Service subscriber, public actor documents and the Service's explicit Bridge
identity declaration supply additional proof of registered-user ownership.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from ..activitypub_models import ActivityPubEvent, activity_actor_id
from ..bridge_policy import BridgePolicyService
from ..db import Database
from ..nodebb_discovery import AP_ACCEPT, SecureNodeBBJSON, _origin

ActorFetch = Callable[[str, str], Awaitable[dict]]
BRIDGE_SOFTWARE_PROPERTY = "Bridge software"
BRIDGE_SOFTWARE_VALUE = "discord-fediverse-bridge"


@dataclass(frozen=True)
class AuthorizedInboundSource:
    """Immutable authorization result for one verified local Group delivery."""

    author_actor_id: str
    subscriber_actor_id: str
    local_community_id: int
    evidence_kind: str


def _actor_origin(actor_id: str, namespace: str) -> str | None:
    """Require a canonical public HTTPS actor under a specific typed namespace."""
    try:
        origin = _origin(actor_id)
        path = urlsplit(actor_id)
    except Exception:
        return None
    prefix = f"/actors/{namespace}/"
    identifier = path.path.removeprefix(prefix)
    if (
        not path.path.startswith(prefix) or not identifier
        or "/" in identifier or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in identifier)
        or path.query or path.fragment or actor_id != f"{origin}{path.path}"
    ):
        return None
    return origin


def _key_is_owned_by(actor: dict, actor_id: str) -> bool:
    """Require an actor-controlled advertised key, not an unrelated key URL."""
    raw = actor.get("publicKey")
    keys = raw if isinstance(raw, list) else [raw]
    return any(
        isinstance(key, dict)
        and key.get("owner") == actor_id
        and isinstance(key.get("id"), str)
        and key["id"].startswith(actor_id + "#")
        and isinstance(key.get("publicKeyPem"), str)
        and bool(key["publicKeyPem"].strip())
        for key in keys
    )


def _valid_actor_document(actor: dict, actor_id: str, actor_type: str, origin: str) -> bool:
    """Check the canonical actor and routing/key fields actually emitted by Bridge."""
    endpoints = actor.get("endpoints")
    return (
        actor.get("id") == actor_id
        and actor.get("type") == actor_type
        and actor.get("inbox") == actor_id + "/inbox"
        and actor.get("outbox") == actor_id + "/outbox"
        and isinstance(endpoints, dict)
        and endpoints.get("sharedInbox") == origin + "/inbox"
        and _key_is_owned_by(actor, actor_id)
    )


def _declares_bridge_service(actor: dict) -> bool:
    """Require an explicit public software declaration on the verified Service."""
    attachments = actor.get("attachment")
    items = attachments if isinstance(attachments, list) else [attachments]
    return any(
        isinstance(item, dict)
        and item.get("type") == "PropertyValue"
        and item.get("name") == BRIDGE_SOFTWARE_PROPERTY
        and item.get("value") == BRIDGE_SOFTWARE_VALUE
        for item in items
    )


def _verified_outer_person(event: ActivityPubEvent, activity_type: str = "Create") -> bool:
    """Require an owner-signed direct action with matching source provenance.

    Fedify authenticates the outer activity before forwarding it to Python.
    A Group Announce does not separately authenticate the embedded Person,
    and an Update/Delete must never borrow authorization from an earlier Create.
    """
    if (
        event.source_announce_id is not None
        or event.verified_sender_actor_id != event.actor_id
    ):
        return False
    source = event.source_activity_json
    # Fedify may expand the verified actor while resolving its display name;
    # compare its ID with the authenticated sender in either representation.
    source_actor = activity_actor_id(source.get("actor")) if isinstance(source, dict) else None
    if (
        not isinstance(source, dict)
        or source.get("type") != activity_type
        or source_actor != event.actor_id
    ):
        return False
    obj = source.get("object")
    # A signed Delete may contain only the object IRI or a Tombstone, whereas
    # Create/Update usually carry an attributed Note/Page. None of these fresh
    # fields can change the canonical owner stored by the original Create.
    if isinstance(obj, str):
        # Create/Update need an attributed object; only Delete may legally
        # refer to an ID-only Tombstone without fresh object attribution.
        if activity_type != "Delete" or obj != event.object.ap_id:
            return False
    elif isinstance(obj, dict):
        if obj.get("id") != event.object.ap_id:
            return False
        if obj.get("attributedTo") not in (None, event.actor_id):
            return False
        if activity_type == "Create" and obj.get("attributedTo") != event.actor_id:
            return False
        if activity_type in ("Update", "Delete") and obj.get("inReplyTo") is not None:
            # Signed ancestry is authoritative. Never authorize a modification
            # against a normalized parent that hides a different signed parent.
            if (event.object.kind != "comment"
                    or obj["inReplyTo"] != event.object.parent_ap_id):
                return False
    else:
        return False
    return event.object.attributed_to_actor_id in (None, event.actor_id)


async def authorize_inbound_source(
    *,
    event: ActivityPubEvent,
    local_community_id: int,
    database: Database,
    policy: BridgePolicyService,
    fetch_actor: ActorFetch | None = None,
    source_activity_type: str = "Create",
) -> AuthorizedInboundSource | None:
    """Resolve one accepted tenant subscriber without trusting an actor hostname.

    Ordinary same-actor subscribers retain their existing authorization.  A
    Person using a Service relationship must have the verified outer sender,
    canonical Bridge actor documents, and exactly one accepted Service on the
    same origin.  Re-read that relationship after network I/O so an Undo during
    actor fetching cannot authorize content through a stale accepted row.
    """
    author = event.actor_id
    if source_activity_type not in ("Create", "Update", "Delete"):
        return None
    # Mutations require their own verified owner action even for a legacy
    # same-actor follower. A previous Create signature never authorizes a fresh
    # modification and Announce(inner Update) carries no independent signature.
    if source_activity_type != "Create" and not _verified_outer_person(event, source_activity_type):
        return None
    if event.verified_sender_actor_id is not None and event.verified_sender_actor_id != author:
        return None
    # Even an accepted same-actor subscriber cannot publish an object with a
    # contradictory original author. Later mutation admission relies on this
    # Create-time provenance rather than a fresh, attacker-controlled envelope.
    if event.object.attributed_to_actor_id not in (None, author):
        return None
    direct = database.remote_subscribers.get_remote_subscriber(
        local_community_id=local_community_id, remote_actor_id=author,
    )
    if direct is not None and direct.status == "accepted":
        return AuthorizedInboundSource(author, author, local_community_id, "direct_subscriber")

    # A wrapped Create is signed by its outer distributor, not by its inner
    # Person. Never grant Service rights based on unsigned inner attribution.
    if not _verified_outer_person(event, source_activity_type):
        return None
    origin = _actor_origin(author, "users")
    if origin is None or not policy.federation_decision(author).allowed:
        return None
    services = [
        row for row in database.remote_subscribers.list_remote_subscribers(
            local_community_id, status="accepted"
        ) if _actor_origin(row.remote_actor_id, "services") == origin
    ]
    # Multiple accepted Services on one origin give no unambiguous original
    # subscriber to exclude from relay; do not choose by insertion order.
    if len(services) != 1:
        return None
    subscriber = services[0]
    if not (
        policy.federation_decision(subscriber.remote_actor_id).allowed
        and policy.federation_decision(subscriber.remote_inbox_url).allowed
    ):
        return None
    get = fetch_actor or SecureNodeBBJSON(origin, policy)
    try:
        person = await get(author, AP_ACCEPT)
        service = await get(subscriber.remote_actor_id, AP_ACCEPT)
    except Exception:
        # DNS, HTTP, JSON and policy failures must never degrade to same-host
        # trust. The event can be delivered again after discovery recovers.
        return None
    if not (
        isinstance(person, dict) and isinstance(service, dict)
        and _valid_actor_document(person, author, "Person", origin)
        and _valid_actor_document(service, subscriber.remote_actor_id, "Service", origin)
        and _declares_bridge_service(service)
        and subscriber.remote_inbox_url == service["inbox"]
    ):
        return None
    current = database.remote_subscribers.get_remote_subscriber(
        local_community_id=local_community_id, remote_actor_id=subscriber.remote_actor_id,
    )
    if (
        current is None or current.status != "accepted"
        or current.id != subscriber.id
        or current.follow_activity_id != subscriber.follow_activity_id
        or current.remote_inbox_url != subscriber.remote_inbox_url
    ):
        return None
    return AuthorizedInboundSource(author, subscriber.remote_actor_id, local_community_id, "bridge_service_person")
