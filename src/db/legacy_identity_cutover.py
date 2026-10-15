"""One-time released ActivityPub identity conversion, with explicit relationship policy.

Only supported same-origin identities/routing fields are edited. Source planning
is pure; the caller owns the connection, transaction, preservation and version.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from urllib.parse import urlsplit, unquote

from ..activitypub_identity_urls import LocalActorKind, build_actor_address


@dataclass(frozen=True)
class IdentityCutoverConfig:
    """Carry only deployment's non-secret target origin and service identifier."""

    public_origin: str
    service_actor_identifier: str


# Audit/body fields are historical evidence, not URLs for routing or lookup.
IDENTITY_FIELDS = {
    "actor_url",
    "inbox_url",
    "outbox_url",
    "followers_url",
    "key_id",
    "community_actor_url",
    "community_actor_id",
    "lemmy_community_actor_id",
    "community_inbox_url",
    "object_id",
    "activity_id",
    "ap_object_id",
    "ap_activity_id",
    "in_reply_to_object_id",
    "parent_ap_object_id",
    "object_ap_id",
    "source_object_ap_id",
    "source_activity_id",
    "origin_remote_actor_id",
    "target_remote_actor_id",
    "target_inbox_url",
    "remote_actor_id",
    "remote_inbox_url",
    "relay_activity_id",
    "source_announce_id",
    "conversation_root_object_id",
    "lemmy_post_ap_id",
    "lemmy_comment_ap_id",
    "lemmy_parent_comment_ap_id",
}
JSON_ROUTING_KEYS = {
    "@id",
    "id",
    "actor",
    "object",
    "attributedTo",
    "inReplyTo",
    "to",
    "cc",
    "bto",
    "bcc",
    "audience",
    "target",
    "context",
    "inbox",
    "outbox",
    "followers",
    "owner",
    "creator",
}


def _origin(value: str) -> str:
    """Validate origin-only comparison components without treating a path as an origin."""
    p = urlsplit(value)
    if p.scheme not in {"http", "https"} or not p.netloc or p.username or p.password:
        raise RuntimeError("Invalid identity origin")
    host = p.hostname
    if host is None:
        raise RuntimeError("Invalid identity origin")
    host = "[" + host + "]" if ":" in host else host
    return f"{p.scheme}://{host}" + (f":{p.port}" if p.port is not None else "")


def _actor_target(row, kind, field, config):
    """Classify a registered actor without guessing a new origin or identifier."""
    old = row["actor_url"]
    origin = _origin(old)
    if config is not None:
        if (
            _origin(config.public_origin) != origin
            or urlsplit(config.public_origin).path not in {"", "/"}
            or urlsplit(config.public_origin).query
            or urlsplit(config.public_origin).fragment
        ):
            raise RuntimeError(
                f'Unsupported identity origin change: {kind} row {row["id"]}'
            )
    identifier = (
        row[field]
        if field
        else (
            config.service_actor_identifier
            if config
            else unquote(urlsplit(old).path.split("/")[-1])
        )
    )
    address = build_actor_address(origin=origin, kind=kind, identifier=identifier)
    legacy = (
        f"{origin}/communities/{identifier}"
        if kind == LocalActorKind.COMMUNITY
        else f"{origin}/actors/{identifier}"
    )
    accepted = {address.actor_url, legacy}
    if kind == LocalActorKind.SERVICE:
        # The singleton key row proves source ownership. Deployment configuration
        # supplies the target name; do not infer it from a stale generic route.
        parts = urlsplit(old).path.strip("/").split("/")
        if (len(parts) == 2 and parts[0] == "actors") or (
            len(parts) == 3 and parts[:2] == ["actors", "services"]
        ):
            source_name = unquote(parts[-1])
            source = build_actor_address(
                origin=origin, kind=kind, identifier=source_name
            )
            accepted.update({source.actor_url, f"{origin}/actors/{source_name}"})
    if old not in accepted:
        raise RuntimeError(f'Unknown legacy actor identity: {kind} row {row["id"]}')
    if old != address.actor_url and config is None:
        raise RuntimeError(
            "Legacy identity cutover requires public origin and service actor configuration"
        )
    return address


def plan_identity_cutover(
    snapshot: dict, config: IdentityCutoverConfig | None
) -> tuple[dict, dict]:
    """Compute the permitted transformed original rows before any normalization writes."""
    expected = deepcopy(snapshot)
    urls = {}
    actors = {}
    changed_communities = set()
    service_changed = False
    users = {}
    communities = {}
    primary_targets = {}
    origins = set()

    def record(old, new, *, primary=False):
        """Reject overlapping owners/content IDs before sequential SQL updates could collide."""
        if old in urls and urls[old] != new:
            raise RuntimeError(f"Ambiguous legacy identity mapping: {old}")
        if primary and new in primary_targets and primary_targets[new] != old:
            raise RuntimeError(f"Identity collision for target {new}")
        if primary:
            primary_targets[new] = old
        urls[old] = new

    for table, kind, field in [
        ("users", LocalActorKind.USER, "activitypub_username"),
        ("local_communities", LocalActorKind.COMMUNITY, "slug"),
        ("bridge_actor_keys", LocalActorKind.SERVICE, None),
    ]:
        for old, new in zip(snapshot[table], expected[table], strict=True):
            address = _actor_target(old, kind, field, config)
            # Validate ownership before recording aliases. Otherwise a foreign
            # collection/key URL would be silently claimed by the local actor.
            source_name = (
                old[field]
                if field
                else unquote(urlsplit(old["actor_url"]).path.split("/")[-1])
            )
            source = build_actor_address(
                origin=_origin(old["actor_url"]), kind=kind, identifier=source_name
            )
            legacy_root = (
                f'{_origin(old["actor_url"])}/communities/{source_name}'
                if kind == LocalActorKind.COMMUNITY
                else f'{_origin(old["actor_url"])}/actors/{source_name}'
            )
            for column, suffix in [
                ("inbox_url", "/inbox"),
                ("outbox_url", "/outbox"),
                ("followers_url", "/followers"),
                ("key_id", "#main-key"),
            ]:
                if column in old and old[column] not in {
                    legacy_root + suffix,
                    source.actor_url + suffix,
                }:
                    raise RuntimeError(
                        f'Conflicting source actor metadata: {table} row {old["id"]} column {column}'
                    )
            origins.add(_origin(old["actor_url"]))
            actor = address.actor_url
            actors[old["actor_url"]] = actor
            record(old["actor_url"], actor)
            if kind == LocalActorKind.USER:
                users[old[field]] = actor
            elif kind == LocalActorKind.COMMUNITY:
                communities[old[field]] = actor
                if old["actor_url"] != actor:
                    changed_communities.add(old["id"])
            else:
                service_changed = old["actor_url"] != actor
            for column in ["actor_url", "inbox_url", "outbox_url", "followers_url"]:
                if column in old:
                    target = getattr(address, column)
                    record(old[column], target)
                    new[column] = target
            if table == "bridge_actor_keys":
                record(old["key_id"], address.public_key_url)
                new["key_id"] = address.public_key_url
    if len(origins) > 1:
        raise RuntimeError("Ambiguous legacy local identity origins")
    if (
        config
        and (
            snapshot["bridge_actor_follows"]
            or snapshot["channel_community_subscriptions"]
        )
        and not snapshot["bridge_actor_keys"]
    ):
        raise RuntimeError(
            "Legacy outbound identity requires persisted service key ownership"
        )

    # Content validation performs constant-time registered-owner lookups.
    registered_actors = set(actors.values())

    def validate_primary(value, *, kind, activity, expected_owner):
        """Validate local content grammar and explicit ownership, preserving remote IDs."""
        if value is None:
            return
        if not isinstance(value, str) or not value.startswith(("http://", "https://")):
            raise RuntimeError("Unsupported local content identity: non-absolute canonical ID")
        if _origin(value) not in origins:
            return
        p = urlsplit(value)
        parts = p.path.strip("/").split("/")
        # Exact aliases must also satisfy the content contract; an actor or
        # collection URL recorded earlier is not a canonical object/activity ID.
        owner = _origin(value) + "/" + "/".join(parts[:3])
        tail = parts[3:]
        content_kind = {"posts": "post", "comments": "comment"}
        valid = (
            owner in registered_actors
            and parts[:2] in [["actors", "users"], ["actors", "communities"]]
            and not p.query
            and not p.fragment
            and p.path == "/" + "/".join(parts)
            and all(parts)
        )
        if activity:
            valid = valid and (
                len(tail) == 4
                and tail[0] == "activities"
                and tail[1] in {"create", "update", "delete", "announce"}
                and (tail[1] != "announce" or parts[1] == "communities")
                and tail[2] in content_kind
                and (kind is None or content_kind[tail[2]] == kind)
            )
        else:
            valid = valid and (
                len(tail) == 3
                and tail[0] == "objects"
                and tail[1] in content_kind
                and (kind is None or content_kind[tail[1]] == kind)
            )
        if not valid:
            raise RuntimeError(f"Unsupported local content identity: {value}")
        if expected_owner is not None and owner != expected_owner:
            raise RuntimeError(f"Local content owner mismatch: {value}")

    def rewrite(value, *, kind=None, actor=None, activity=False, canonical=False):
        """Rewrite declared tag namespaces, preserving remote and already-typed IDs."""
        if not isinstance(value, str):
            return value
        if value in urls:
            return urls[value]
        if (
            not value.startswith(("http://", "https://"))
            or _origin(value) not in origins
        ):
            return value
        p = urlsplit(value)
        parts = p.path.strip("/").split("/")
        if canonical and (p.path != "/" + "/".join(parts) or not all(parts)):
            raise RuntimeError(f"Unsupported local content identity: {value}")
        if (
            parts[:1] == ["actors"]
            and len(parts) >= 3
            and parts[1] in {"users", "communities", "services"}
        ):
            return value
        target = None
        tail = []
        if len(parts) >= 2 and parts[0] in {"users", "communities"}:
            target = (users if parts[0] == "users" else communities).get(
                unquote(parts[1])
            )
            if target is None:
                raise RuntimeError(f"Unknown local legacy owner: {value}")
            tail = parts[2:]
        if target and tail in [[], ["inbox"], ["outbox"], ["followers"]]:
            return target + ("/" + tail[0] if tail else "")
        if p.query or p.fragment:
            raise RuntimeError(f"Ambiguous legacy content identity: {value}")
        if target and len(tail) == 2 and tail[0] in {"post", "comment"}:
            return (
                target
                + "/objects/"
                + ("posts" if tail[0] == "post" else "comments")
                + "/"
                + tail[1]
            )
        if (
            target
            and len(tail) == 4
            and tail[0] == "activities"
            and tail[1] in {"create", "update", "delete"}
            and tail[2] in {"post", "comment"}
        ):
            return (
                target
                + "/activities/"
                + tail[1]
                + "/"
                + ("posts" if tail[2] == "post" else "comments")
                + "/"
                + tail[3]
            )
        if (
            target and kind in {"post", "comment"}
            and (
                len(tail) == 2 and tail[0] == "activities"
                or len(tail) == 3 and tail[:2] in [
                    ["activities", "announce"], ["activities", "delete"]
                ]
            )
        ):
            action = tail[1] if len(tail) == 3 else "create"
            return (
                target
                + "/activities/"
                + action
                + "/"
                + ("posts" if kind == "post" else "comments")
                + "/"
                + tail[-1]
            )
        if (
            actor
            and parts[:1] in [["activities"], ["objects"]]
            and kind not in {"post", "comment"}
        ):
            raise RuntimeError(f"Ambiguous legacy content kind: {value}")
        if (
            actor
            and kind in {"post", "comment"}
            and len(parts) == 2 and all(parts)
            and parts[:1] in [["activities"], ["objects"]]
        ):
            return (
                actor
                + ("/activities/create/" if activity else "/objects/")
                + ("posts" if kind == "post" else "comments")
                + "/"
                + parts[-1]
            )
        if parts[:1] in [["users"], ["communities"], ["actors"]]:
            raise RuntimeError(f"Unsupported legacy local identity: {value}")
        if canonical:
            raise RuntimeError(f"Unsupported local content identity: {value}")
        return value

    # Primary identities establish exact maps before parent/reference processing.
    thread_community = {
        r["id"]: r["local_community_id"] for r in snapshot["local_community_threads"]
    }
    community_by_id = {
        r["id"]: communities[r["slug"]] for r in snapshot["local_communities"]
    }
    for table, object_field, activity_field, kind_hint in [
        ("published_activity_objects", "object_id", "activity_id", None),
        ("local_community_threads", "ap_object_id", "ap_activity_id", "post"),
        ("local_community_messages", "ap_object_id", "ap_activity_id", "comment"),
        ("community_thread_groups", "ap_object_id", "ap_activity_id", "post"),
        ("community_message_groups", "ap_object_id", "ap_activity_id", "comment"),
        ("message_mappings", "object_id", "activity_id", None),
        ("local_community_relay_deliveries", None, "relay_activity_id", None),
    ]:
        for old, new in zip(snapshot[table], expected[table], strict=True):
            kind = kind_hint or old.get("kind", old.get("object_kind"))
            actor = actors.get(old.get("actor_url"))
            # Publication/outbound mapping actors are authors; inbound mapping
            # actors may be relayers. A thread's community is routing context,
            # not its author. Relay activities themselves belong to the community.
            expected_owner = None
            if table == "published_activity_objects" or (
                table == "message_mappings" and old["source_platform"] == "discord"
            ):
                expected_owner = urls.get(old["actor_url"], old["actor_url"])
            elif table == "local_community_relay_deliveries":
                expected_owner = community_by_id.get(old["local_community_id"])
            if table == "message_mappings":
                parts = urlsplit(old["object_id"]).path.strip("/").split("/")
                if (
                    len(parts) == 4
                    and parts[0] in {"users", "communities"}
                    and parts[2] in {"post", "comment"}
                ):
                    kind = parts[2]
            if table in {"local_community_threads", "local_community_messages"}:
                cid = old.get(
                    "local_community_id",
                    thread_community.get(old.get("local_community_thread_id")),
                )
                actor = community_by_id.get(cid) if cid in changed_communities else None
            for field, is_activity in [(object_field, False), (activity_field, True)]:
                if field is None:
                    continue  # Relay deliveries own an activity, not the remote source object.
                value = old[field]
                target = rewrite(
                    value, kind=kind, actor=actor, activity=is_activity, canonical=True
                )
                validate_primary(
                    target, kind=kind, activity=is_activity, expected_owner=expected_owner
                )
                if value is not None:
                    record(value, target, primary=True)
                new[field] = target
    for table, rows in expected.items():
        for row in rows:
            for field, value in list(row.items()):
                if field in IDENTITY_FIELDS:
                    row[field] = rewrite(value, kind=row.get("object_kind"))
                elif field == "source_id" and row.get("source_platform") != "discord":
                    row[field] = urls.get(value, value)
                elif (
                    table == "local_community_relay_source_activities"
                    and field == "source_activity_json"
                    and value is not None
                ):
                    parsed = json.loads(value)
                    converted = _rewrite_json(
                        parsed,
                        lambda value: rewrite(value, kind=row.get("object_kind")),
                    )
                    if converted != parsed:
                        if _signed_json(parsed):
                            raise RuntimeError(
                                f'Signed JSON identity mutation unsupported: {table} row {row["id"]}'
                            )
                        row[field] = json.dumps(
                            converted, separators=(",", ":"), ensure_ascii=False
                        )
    # Only incoming confirmations for renamed local community actors are lost.
    expected["remote_subscribers"] = [
        r
        for r in expected["remote_subscribers"]
        if r["local_community_id"] not in changed_communities
    ]
    changed_targets = {
        r["actor_url"]
        for r in snapshot["local_communities"]
        if r["id"] in changed_communities
    }
    for table in ["bridge_actor_follows", "channel_community_subscriptions"]:
        field = (
            "community_actor_id"
            if table == "bridge_actor_follows"
            else "lemmy_community_actor_id"
        )
        for old, new in zip(snapshot[table], expected[table], strict=True):
            if service_changed or old[field] in changed_targets:
                new["status"] = "failed"
                new["follow_activity_id"] = None
    # Source-declared uniqueness includes cache and relay identities as well as
    # primary content URLs. Detect collisions before any normalization writes.
    from .legacy_stage00_ddl import RELEASE_MANIFEST

    for table, manifest in RELEASE_MANIFEST.items():
        for fields in manifest["unique"]:
            seen = set()
            for row in expected[table]:
                values = tuple(row[field] for field in fields)
                if any(value is None for value in values):
                    continue  # SQLite UNIQUE permits repeated NULL-containing keys.
                if values in seen:
                    raise RuntimeError(
                        f"Identity collision: {table} row {row['id']} columns {fields}"
                    )
                seen.add(values)
    return expected, urls


def _rewrite_json(value, rewrite, key=None):
    """Edit JSON identity/routing values without changing body text or dictionary keys."""
    if isinstance(value, dict):
        return {k: _rewrite_json(v, rewrite, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite_json(v, rewrite, key) for v in value]
    if isinstance(value, str) and key in JSON_ROUTING_KEYS:
        return rewrite(value)
    return value


def _signed_json(value):
    """Recognize signatures/proofs whose covered payload must not be silently rewritten."""
    if isinstance(value, dict):
        return any(
            k in {"signature", "proof"} or _signed_json(v) for k, v in value.items()
        )
    if isinstance(value, list):
        return any(_signed_json(v) for v in value)
    return False


def apply_identity_cutover(conn, original: dict, expected: dict, urls: dict) -> None:
    """Apply only the planned row/column delta on the caller's transaction connection."""
    for table, rows in original.items():
        targets = {r["id"]: r for r in expected[table]}
        for old in rows:
            new = targets.get(old["id"])
            if new is None:
                if table != "remote_subscribers":
                    raise RuntimeError("Unapproved identity deletion")
                conn.exec_driver_sql(
                    "DELETE FROM remote_subscribers WHERE id=?", (old["id"],)
                )
                continue
            changes = {k: v for k, v in new.items() if v != old[k]}
            if changes:
                conn.exec_driver_sql(
                    f'UPDATE "{table}" SET '
                    + ",".join(f'"{k}"=?' for k in changes)
                    + " WHERE id=?",
                    tuple(changes.values()) + (old["id"],),
                )
    # This normalized column did not exist in v0; root links use the exact map.
    for old, new in urls.items():
        if old != new:
            conn.exec_driver_sql(
                "UPDATE published_activity_objects SET conversation_root_object_id=? WHERE conversation_root_object_id=?",
                (new, old),
            )


def validate_current_identity(conn, config: IdentityCutoverConfig | None) -> None:
    """Reject already-stamped identities mismatching deployment settings, without repairs."""
    for table, kind, field in [
        ("users", LocalActorKind.USER, "activitypub_username"),
        ("local_communities", LocalActorKind.COMMUNITY, "slug"),
        ("bridge_actor_keys", LocalActorKind.SERVICE, None),
    ]:
        for row in conn.exec_driver_sql(f"SELECT * FROM {table}").mappings():
            address = _actor_target(row, kind, field, config)
            for column in [
                "actor_url",
                "inbox_url",
                "outbox_url",
                "followers_url",
                "key_id",
            ]:
                if column in row and row[column] != (
                    address.public_key_url
                    if column == "key_id"
                    else getattr(address, column)
                ):
                    raise RuntimeError(
                        f'Current v1 has incompatible actor identity: {table} row {row["id"]}; restore v0 backup'
                    )
