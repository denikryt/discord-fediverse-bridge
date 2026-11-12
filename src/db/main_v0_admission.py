"""Read-only identity evidence for exact known-main and release-v0 SQLite.

Source schema recognition alone cannot prove that the unversioned main
instance has completed its one-time typed-actor cutover. This module compares
all persisted local actor address families and rejects mixed/foreign owners
rather than guessing from a single local URL.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from urllib.parse import urlsplit
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..activitypub_identity_urls import LocalActorKind, build_actor_address

if TYPE_CHECKING:
    from .migrations import Stage4IdentityMigrationConfig


@dataclass(frozen=True, slots=True)
class LegacyIdentityEvidence:
    """Explicit observed local actor state and affected relationship owners."""

    state: str
    changed_user_ids: tuple[int, ...]
    changed_local_community_ids: tuple[int, ...]
    service_actor_changed: bool


def inspect_known_main_identities(
    conn: Connection, config: Stage4IdentityMigrationConfig | None,
) -> LegacyIdentityEvidence:
    """Classify all local actor rows, refusing any mixed or unsupported states.

    ``empty`` means no local actor/owner evidence was saved and therefore no
    relationship may be fabricated. ``typed`` has no actor change. ``legacy``
    has a reproducible conversion using the same immutable owner identifiers.
    """
    identity_rows: list[tuple[str, int, bool]] = []
    if config is None:
        for table in ("users", "local_communities", "bridge_actor_keys"):
            if conn.execute(text(f"SELECT EXISTS(SELECT 1 FROM {table})")).scalar():
                raise RuntimeError("local identity evidence requires public origin and Service identifier")
        if conn.execute(text("SELECT EXISTS(SELECT 1 FROM bridge_actor_follows) ")).scalar():
            raise RuntimeError("outgoing Follow requires verified Service identity evidence")
        return LegacyIdentityEvidence("empty", (), (), False)

    origin = config.public_origin.rstrip("/")
    # Building an address also checks the supported local actor grammar.
    service = build_actor_address(
        origin=origin, kind=LocalActorKind.SERVICE,
        identifier=config.service_actor_identifier,
    )
    for table, id_field, identifier_field, kind, legacy_segment in (
        ("users", "id", "activitypub_username", LocalActorKind.USER, "actors"),
        ("local_communities", "id", "slug", LocalActorKind.COMMUNITY, "communities"),
    ):
        for row in conn.execute(text(f"""
            SELECT {id_field} AS owner_id, {identifier_field} AS identifier,
                actor_url, inbox_url, outbox_url, followers_url FROM {table}
        """)).mappings():
            address = build_actor_address(
                origin=origin, kind=kind, identifier=str(row["identifier"]),
            )
            old = f"{origin}/{legacy_segment}/{row['identifier']}"
            current = str(row["actor_url"])
            if current == old:
                typed = False
            elif current == address.actor_url:
                typed = True
            else:
                raise RuntimeError(f"unsupported {table} actor identity owner={row['owner_id']}")
            required = {
                "inbox_url": f"{current}/inbox", "outbox_url": f"{current}/outbox",
                "followers_url": f"{current}/followers",
            }
            if any(row[field] != value for field, value in required.items()):
                raise RuntimeError(f"mixed {table} actor endpoints owner={row['owner_id']}")
            identity_rows.append((table, int(row["owner_id"]), typed))

    for row in conn.execute(text("SELECT id, actor_url, key_id FROM bridge_actor_keys")).mappings():
        old = f"{origin}/actors/{config.service_actor_identifier}"
        typed = str(row["actor_url"]) == service.actor_url
        if row["actor_url"] != old and not typed:
            raise RuntimeError("unsupported persisted Bridge Service identity")
        if row["key_id"] != f"{row['actor_url']}#main-key":
            raise RuntimeError("mixed Bridge Service actor/key identity")
        identity_rows.append(("bridge_actor_keys", int(row["id"]), typed))

    states = {typed for _table, _id, typed in identity_rows}
    if len(states) > 1:
        raise RuntimeError("mixed unversioned main actor namespaces; manual backup review required")
    has_follow = bool(conn.execute(text(
        "SELECT EXISTS(SELECT 1 FROM bridge_actor_follows)"
    )).scalar())
    if has_follow and not any(table == "bridge_actor_keys" for table, _id, _typed in identity_rows):
        raise RuntimeError("outgoing Follow lacks original Service key owner evidence")
    if not identity_rows:
        _check_stored_identity_provenance(conn, origin, "empty")
        return LegacyIdentityEvidence("empty", (), (), False)
    is_typed = states == {True}
    _check_stored_identity_provenance(conn, origin, "typed" if is_typed else "legacy")
    return LegacyIdentityEvidence(
        "typed" if is_typed else "legacy",
        tuple(id for table, id, typed in identity_rows if table == "users" and not typed),
        tuple(id for table, id, typed in identity_rows if table == "local_communities" and not typed),
        any(table == "bridge_actor_keys" and not typed for table, _id, typed in identity_rows),
    )


_STORED_IDENTITY_COLUMNS: dict[str, tuple[str, ...]] = {
    "published_activity_objects": (
        "actor_url", "community_actor_url", "activity_id", "object_id",
        "in_reply_to_object_id", "conversation_root_object_id",
    ),
    "message_mappings": (
        "actor_url", "community_actor_url", "activity_id", "object_id",
    ),
    "local_community_threads": ("ap_activity_id", "ap_object_id"),
    "local_community_messages": ("ap_activity_id", "ap_object_id", "parent_ap_object_id"),
    "community_thread_groups": ("community_actor_id", "ap_activity_id", "ap_object_id"),
    "community_message_groups": ("community_actor_id", "ap_activity_id", "ap_object_id"),
    "user_actor_outbound_sources": ("activity_id", "snapshot_json", "activity_json"),
    "user_actor_content_mutations": ("activity_id", "request_json", "canonical_activity_json"),
    "local_community_relay_deliveries": ("relay_activity_id",),
}


def _iter_scalar_strings(item: object):
    """Visit JSON leaf values without guessing which field establishes provenance."""
    if isinstance(item, str):
        yield item
    elif isinstance(item, list):
        for value in item:
            yield from _iter_scalar_strings(value)
    elif isinstance(item, dict):
        for value in item.values():
            yield from _iter_scalar_strings(value)


def _check_stored_identity_provenance(conn: Connection, origin: str, state: str) -> None:
    """Reject mixed historical object/JSON identities before any DDL/row writes."""
    allowed_origin = urlsplit(origin)
    for table, candidate_columns in _STORED_IDENTITY_COLUMNS.items():
        actual = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
        names = [name for name in candidate_columns if name in actual]
        if not names:
            continue
        # Streaming iteration avoids buffering an entire production outbox in RAM.
        for row in conn.exec_driver_sql(f"SELECT {','.join(names)} FROM {table}"):
            for column, raw in zip(names, row):
                if raw is None:
                    continue
                items: object = raw
                if column.endswith("_json") and isinstance(raw, str):
                    try:
                        items = json.loads(raw)
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError(f"unverifiable persisted JSON in {table}.{column}") from exc
                for value in _iter_scalar_strings(items):
                    try:
                        parsed = urlsplit(value)
                    except ValueError:
                        continue
                    if (parsed.scheme, parsed.hostname, parsed.port) != (
                        allowed_origin.scheme, allowed_origin.hostname, allowed_origin.port
                    ):
                        continue
                    if parsed.username is not None or parsed.password is not None:
                        raise RuntimeError(f"untrusted local URL credentials in {table}.{column}")
                    path = parsed.path
                    is_typed = any(path.startswith(prefix) for prefix in (
                        "/actors/users/", "/actors/communities/", "/actors/services/",
                    ))
                    is_legacy = any(path.startswith(prefix) for prefix in (
                        "/communities/", "/c/", "/users/", "/activities/",
                    )) or path.startswith("/actors/") and not is_typed
                    if state == "typed" and is_legacy:
                        raise RuntimeError(f"legacy local object identity in {table}.{column}")
                    if state == "legacy" and is_typed:
                        raise RuntimeError(f"typed object identity mixed with legacy owner in {table}.{column}")
                    if state == "empty" and (is_legacy or is_typed):
                        raise RuntimeError(f"local object has no verified actor owner in {table}.{column}")
