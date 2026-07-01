"""Stage 4 typed ActivityPub identity migration behavior tests."""

from __future__ import annotations

from sqlalchemy import text

from src.db import Database
from src.db.migrations import Stage4IdentityMigrationConfig


def _rows(connection, table: str) -> list[dict[str, object]]:
    """Return stable dictionaries for byte-for-byte preservation assertions."""
    return [dict(row) for row in connection.execute(text(f"SELECT * FROM {table} ORDER BY id")).mappings()]


def test_stage4_migration_uses_configured_service_identifier_and_preserves_surfaces(tmp_path) -> None:
    """Migrate retained local identity while preserving Discord surface mappings."""
    path = tmp_path / "stage4.db"
    database = Database(f"sqlite:///{path}")
    database.create_all()
    now = "2026-06-21T12:00:00Z"
    with database.engine.begin() as connection:
        connection.execute(text("INSERT INTO users (id, discord_user_id, activitypub_username, actor_url, inbox_url, outbox_url, followers_url, public_key_pem, private_key_pem, profile_version, created_at, updated_at) VALUES (1, '1', 'alice', 'https://bridge.example/actors/users/alice', 'https://bridge.example/actors/users/alice/inbox', 'https://bridge.example/actors/users/alice/outbox', 'https://bridge.example/actors/users/alice/followers', 'public', 'private', 3, :now, :now)"), {"now": now})
        connection.execute(text("INSERT INTO local_communities (id, discord_guild_id, discord_forum_channel_id, slug, display_name, actor_url, inbox_url, outbox_url, followers_url, public_key_pem, private_key_pem, status, created_at, updated_at) VALUES (2, 10, 20, 'python', 'Python', 'https://bridge.example/actors/communities/python', 'https://bridge.example/actors/communities/python/inbox', 'https://bridge.example/actors/communities/python/outbox', 'https://bridge.example/actors/communities/python/followers', 'public', 'private', 'active', :now, :now)"), {"now": now})
        connection.execute(text("INSERT INTO bridge_actor_keys (id, actor_url, key_id, key_format, algorithm, public_key_data, private_key_data, created_at, updated_at) VALUES (1, 'https://bridge.example/actors/services/bridge', 'https://bridge.example/actors/services/bridge#main-key', 'jwk', 'RSASSA-PKCS1-v1_5', '{}', '{}', :now, :now)"), {"now": now})
        connection.execute(text("INSERT INTO local_community_threads (id, local_community_id, ap_activity_id, ap_object_id, direction, origin_kind, created_at) VALUES (5, 2, 'https://bridge.example/actors/communities/python/activities/create/post/thread-1', 'https://bridge.example/actors/communities/python/post/thread-1', 'outbound', 'discord', :now)"), {"now": now})
        connection.execute(text("INSERT INTO local_community_thread_surfaces (id, local_community_thread_id, discord_forum_channel_id, discord_thread_id, discord_starter_message_id, local_subscriber_id, role, created_at) VALUES (6, 5, 20, 30, 40, NULL, 'host', :now)"), {"now": now})
        connection.execute(text("INSERT INTO local_community_messages (id, local_community_thread_id, ap_activity_id, ap_object_id, parent_ap_object_id, direction, created_at) VALUES (7, 5, 'https://bridge.example/actors/communities/python/activities/create/comment/msg-1', 'https://bridge.example/actors/communities/python/comment/msg-1', 'https://bridge.example/actors/communities/python/post/thread-1', 'outbound', :now)"), {"now": now})
        connection.execute(text("INSERT INTO local_community_message_surfaces (id, local_community_message_id, local_community_thread_surface_id, discord_forum_channel_id, discord_message_id, parent_discord_message_id, local_subscriber_id, role, created_at) VALUES (8, 7, 6, 20, 50, 40, NULL, 'host', :now)"), {"now": now})
        connection.execute(text("INSERT INTO user_actor_followers (id, user_id, remote_actor_id, remote_inbox_url, follow_activity_id, status, owner_notification_state, created_at, updated_at) VALUES (9, 1, 'https://remote.example/actors/users/bob', 'https://remote.example/inbox', 'https://remote.example/follows/1', 'accepted', 'pending', :now, :now)"), {"now": now})
        connection.execute(text("INSERT INTO remote_subscribers (id, local_community_id, remote_actor_id, remote_inbox_url, follow_activity_id, status, created_at, updated_at) VALUES (10, 2, 'https://remote.example/u/bob', 'https://remote.example/inbox', 'https://remote.example/follows/2', 'accepted', :now, :now)"), {"now": now})
        before_thread_surfaces = _rows(connection, "local_community_thread_surfaces")
        before_message_surfaces = _rows(connection, "local_community_message_surfaces")

    config = Stage4IdentityMigrationConfig(
        public_origin="https://bridge.example",
        service_actor_identifier="relay",
    )
    database.migrate(config)
    database.migrate(config)

    with database.engine.connect() as connection:
        user = connection.execute(text("SELECT actor_url, inbox_url, outbox_url, followers_url FROM users WHERE id=1")).mappings().one()
        assert user["actor_url"] == "https://bridge.example/actors/users/alice"
        assert user["inbox_url"] == "https://bridge.example/actors/users/alice/inbox"
        community = connection.execute(text("SELECT actor_url, inbox_url, outbox_url, followers_url FROM local_communities WHERE id=2")).mappings().one()
        assert community["actor_url"] == "https://bridge.example/actors/communities/python"
        key = connection.execute(text("SELECT actor_url, key_id FROM bridge_actor_keys WHERE id=1")).mappings().one()
        assert key["actor_url"] == "https://bridge.example/actors/services/relay"
        assert key["key_id"] == "https://bridge.example/actors/services/relay#main-key"
        thread = connection.execute(text("SELECT ap_activity_id, ap_object_id FROM local_community_threads WHERE id=5")).mappings().one()
        assert thread["ap_activity_id"].startswith("https://bridge.example/actors/communities/python/activities/create/posts/")
        assert thread["ap_object_id"].startswith("https://bridge.example/actors/communities/python/objects/posts/")
        message = connection.execute(text("SELECT ap_activity_id, ap_object_id, parent_ap_object_id FROM local_community_messages WHERE id=7")).mappings().one()
        assert message["ap_activity_id"].startswith("https://bridge.example/actors/communities/python/activities/create/comments/")
        assert message["ap_object_id"].startswith("https://bridge.example/actors/communities/python/objects/comments/")
        assert message["parent_ap_object_id"] == thread["ap_object_id"]
        assert _rows(connection, "local_community_thread_surfaces") == before_thread_surfaces
        assert _rows(connection, "local_community_message_surfaces") == before_message_surfaces
        assert connection.execute(text("SELECT COUNT(*) FROM user_actor_followers")).scalar_one() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM remote_subscribers")).scalar_one() == 0

    database.close()


def test_stage4_migration_requires_identity_config_for_populated_local_rows(tmp_path) -> None:
    """Reject populated local identity rows when startup omits the non-secret config."""
    database = Database(f"sqlite:///{tmp_path / 'missing-config.db'}")
    database.create_all()
    with database.engine.begin() as connection:
        connection.execute(text("INSERT INTO bridge_actor_keys (id, actor_url, key_id, key_format, algorithm, public_key_data, private_key_data, created_at, updated_at) VALUES (1, 'https://bridge.example/actors/services/bridge', 'https://bridge.example/actors/services/bridge#main-key', 'jwk', 'RSASSA-PKCS1-v1_5', '{}', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
    try:
        database.migrate()
    except RuntimeError as error:
        assert "Stage 4 identity migration requires" in str(error)
    else:  # pragma: no cover - defensive because the error is the contract.
        raise AssertionError("missing Stage 4 identity config should fail")
    finally:
        database.close()


def test_stage4_migration_rewrites_user_content_mutations_and_cached_json(tmp_path) -> None:
    """Retained Update/Delete mutation recovery rows must survive the cutover typed."""
    database = Database(f"sqlite:///{tmp_path / 'mutations.db'}")
    database.create_all()
    now = "2026-06-21T12:00:00Z"
    old_object = "https://bridge.example/users/alice/objects/posts/post-1"
    old_activity = "https://bridge.example/users/alice/activities/create/posts/create-1"
    old_update = "https://bridge.example/users/alice/activities/update/posts/op-1"
    request_json = {
        "actorUsername": "alice",
        "publicationContext": "personal_feed",
        "communityActorUrl": None,
        "conversationRootObjectId": old_object,
        "apObjectId": old_object,
        "kind": "post",
        "title": "new",
        "bodyMarkdown": "body",
        "inReplyToObjectId": None,
        "targetInboxUrls": ["https://remote.example/inbox"],
        "mutationTimestamp": "2026-06-21T12:00:00+00:00",
        "operationId": "op-1",
        "activityId": old_update,
    }
    canonical_json = {
        "id": old_update,
        "type": "Update",
        "actor": "https://bridge.example/actors/alice",
        "object": {"id": old_object, "url": old_object},
        "to": ["https://remote.example/users/bob"],
    }
    with database.engine.begin() as connection:
        connection.execute(text("INSERT INTO users (id, discord_user_id, activitypub_username, actor_url, inbox_url, outbox_url, followers_url, public_key_pem, private_key_pem, profile_version, created_at, updated_at) VALUES (1, '1', 'alice', 'https://bridge.example/actors/alice', 'https://bridge.example/actors/alice/inbox', 'https://bridge.example/actors/alice/outbox', 'https://bridge.example/actors/alice/followers', 'public', 'private', 1, :now, :now)"), {"now": now})
        connection.execute(text("INSERT INTO published_activity_objects (id, actor_username, actor_url, publication_context, community_actor_url, activity_id, object_id, kind, title, body_markdown, in_reply_to_object_id, conversation_root_object_id, discord_guild_id, discord_channel_id, discord_message_id, content_revision, desired_kind, discord_reconciliation_needed, discord_reconciliation_attempt_count, published_at, created_at) VALUES (1, 'alice', 'https://bridge.example/actors/alice', 'personal_feed', NULL, :activity, :object, 'post', 'old', 'body', NULL, :object, 10, 20, 30, 1, 'update', 0, 0, :now, :now)"), {"activity": old_activity, "object": old_object, "now": now})
        connection.execute(text("INSERT INTO user_actor_content_mutations (id, published_object_id, user_id, kind, expected_revision, next_revision, operation_id, activity_id, request_json, request_fingerprint, state, canonical_activity_json, side_effect_classification, attempt_count, created_at, updated_at) VALUES (1, 1, 1, 'content_update', 1, 2, 'op-1', :activity_id, :request_json, 'fp', 'canonical_ready', :canonical_json, 'canonical_received', 0, :now, :now)"), {"activity_id": old_update, "request_json": __import__('json').dumps(request_json, separators=(',', ':'), sort_keys=True), "canonical_json": __import__('json').dumps(canonical_json, separators=(',', ':'), sort_keys=True), "now": now})

    config = Stage4IdentityMigrationConfig(public_origin="https://bridge.example", service_actor_identifier="bridge")
    database.migrate(config)
    database.migrate(config)

    with database.engine.connect() as connection:
        row = connection.execute(text("SELECT activity_id, request_json, canonical_activity_json FROM user_actor_content_mutations WHERE id=1")).mappings().one()
        assert row["activity_id"].startswith("https://bridge.example/actors/users/alice/activities/update/posts/")
        assert not row["activity_id"].endswith("/op-1")
        request = __import__('json').loads(row["request_json"])
        assert request["activityId"] == row["activity_id"]
        assert request["operationId"] == row["activity_id"].rsplit("/", 1)[-1]
        assert request["apObjectId"] == "https://bridge.example/actors/users/alice/objects/posts/post-1"
        assert request["conversationRootObjectId"] == request["apObjectId"]
        canonical = __import__('json').loads(row["canonical_activity_json"])
        assert canonical["id"] == row["activity_id"]
        assert canonical["actor"] == "https://bridge.example/actors/users/alice"
        assert canonical["object"]["id"] == request["apObjectId"]
        assert canonical["to"] == ["https://remote.example/users/bob"]
    database.close()


def test_stage4_migration_rewrites_local_community_relay_announce_ids(tmp_path) -> None:
    """Retained relay delivery rows use the canonical announce posts/comments grammar."""
    database = Database(f"sqlite:///{tmp_path / 'relay.db'}")
    database.create_all()
    now = "2026-06-21T12:00:00Z"
    with database.engine.begin() as connection:
        connection.execute(text("INSERT INTO local_communities (id, discord_guild_id, discord_forum_channel_id, slug, display_name, actor_url, inbox_url, outbox_url, followers_url, public_key_pem, private_key_pem, status, created_at, updated_at) VALUES (2, 10, 20, 'python', 'Python', 'https://bridge.example/communities/python', 'https://bridge.example/communities/python/inbox', 'https://bridge.example/communities/python/outbox', 'https://bridge.example/communities/python/followers', 'public', 'private', 'active', :now, :now)"), {"now": now})
        connection.execute(text("INSERT INTO local_community_relay_source_activities (id, local_community_id, object_kind, operation, source_object_ap_id, source_activity_id, source_announce_id, origin_remote_actor_id, source_activity_json, created_at) VALUES (1, 2, 'post', 'create', 'https://remote.example/post/1', 'https://remote.example/create/1', NULL, 'https://remote.example/u/bob', '{}', :now)"), {"now": now})
        connection.execute(text("INSERT INTO local_community_relay_deliveries (id, source_activity_row_id, local_community_id, object_kind, operation, source_object_ap_id, source_activity_id, origin_remote_actor_id, target_remote_actor_id, target_inbox_url, delivery_profile, relay_activity_id, status, attempt_count, created_at, updated_at) VALUES (1, 1, 2, 'post', 'create', 'https://remote.example/post/1', 'https://remote.example/create/1', 'https://remote.example/u/bob', 'https://remote.example/u/alice', 'https://remote.example/inbox', 'threadiverse_group', 'https://bridge.example/communities/python/activities/announce/relay-1', 'delivered', 1, :now, :now)"), {"now": now})

    config = Stage4IdentityMigrationConfig(public_origin="https://bridge.example", service_actor_identifier="bridge")
    database.migrate(config)
    database.migrate(config)

    with database.engine.connect() as connection:
        relay_id = connection.execute(text("SELECT relay_activity_id FROM local_community_relay_deliveries WHERE id=1")).scalar_one()
        assert relay_id == "https://bridge.example/actors/communities/python/activities/announce/posts/relay-1"
    database.close()


def test_stage4_migration_rewrites_user_actor_outbound_source_json(tmp_path) -> None:
    """Ready retained User Actor delivery sources must not emit retired local IDs."""
    database = Database(f"sqlite:///{tmp_path / 'outbound-sources.db'}")
    database.create_all()
    now = "2026-06-21T12:00:00Z"
    old_actor = "https://bridge.example/actors/alice"
    old_object = "https://bridge.example/users/alice/objects/posts/post-1"
    old_create = "https://bridge.example/users/alice/activities/create/posts/create-1"
    old_update = "https://bridge.example/users/alice/activities/update/posts/update-1"
    with database.engine.begin() as connection:
        connection.execute(text("INSERT INTO users (id, discord_user_id, activitypub_username, actor_url, inbox_url, outbox_url, followers_url, public_key_pem, private_key_pem, profile_version, created_at, updated_at) VALUES (1, '1', 'alice', :actor, :inbox, :outbox, :followers, 'public', 'private', 9, :now, :now)"), {"actor": old_actor, "inbox": f"{old_actor}/inbox", "outbox": f"{old_actor}/outbox", "followers": f"{old_actor}/followers", "now": now})
        connection.execute(text("INSERT INTO published_activity_objects (id, actor_username, actor_url, publication_context, community_actor_url, activity_id, object_id, kind, title, body_markdown, in_reply_to_object_id, conversation_root_object_id, discord_guild_id, discord_channel_id, discord_message_id, content_revision, desired_kind, discord_reconciliation_needed, discord_reconciliation_attempt_count, published_at, created_at) VALUES (1, 'alice', :actor, 'personal_feed', NULL, :activity, :object, 'post', 'old', 'body', NULL, :object, 10, 20, 30, 1, 'update', 0, 0, :now, :now)"), {"actor": old_actor, "activity": old_create, "object": old_object, "now": now})
        connection.execute(text("INSERT INTO user_actor_outbound_sources (id, user_id, source_kind, ordering_key, logical_version, activity_id, published_object_id, snapshot_json, activity_json, state, render_attempt_count, created_at, updated_at) VALUES (1, 1, 'content_update', 'content:old', 2, :activity, 1, :snapshot, :activity_json, 'ready', 0, :now, :now)"), {
            "activity": old_update,
            "snapshot": __import__('json').dumps({"actor": old_actor, "objectId": old_object, "remote": "https://remote.example/users/bob"}, separators=(",", ":"), sort_keys=True),
            "activity_json": __import__('json').dumps({"id": old_update, "type": "Update", "actor": old_actor, "object": {"id": old_object, "url": old_object}, "to": ["https://remote.example/users/bob"], "cc": [f"{old_actor}/followers"]}, separators=(",", ":"), sort_keys=True),
            "now": now,
        })

    config = Stage4IdentityMigrationConfig(public_origin="https://bridge.example", service_actor_identifier="bridge")
    database.migrate(config)
    database.migrate(config)

    with database.engine.connect() as connection:
        row = connection.execute(text("SELECT activity_id, snapshot_json, activity_json FROM user_actor_outbound_sources WHERE id=1")).mappings().one()
        assert row["activity_id"] == "https://bridge.example/actors/users/alice/activities/update/posts/update-1"
        snapshot = __import__('json').loads(row["snapshot_json"])
        assert snapshot["actor"] == "https://bridge.example/actors/users/alice"
        assert snapshot["objectId"] == "https://bridge.example/actors/users/alice/objects/posts/post-1"
        assert snapshot["remote"] == "https://remote.example/users/bob"
        activity = __import__('json').loads(row["activity_json"])
        assert activity["id"] == row["activity_id"]
        assert activity["actor"] == "https://bridge.example/actors/users/alice"
        assert activity["object"]["id"] == snapshot["objectId"]
        assert activity["object"]["url"] == snapshot["objectId"]
        assert activity["to"] == ["https://remote.example/users/bob"]
        assert activity["cc"] == ["https://bridge.example/actors/users/alice/followers"]
    database.close()


def test_stage4_migration_rewrites_community_sync_group_local_user_ids(tmp_path) -> None:
    """Startup migration rewrites retained community-sync local user AP IDs."""
    database = Database(f"sqlite:///{tmp_path / 'community-sync-groups.db'}")
    database.create_all()
    now = "2026-06-21T12:00:00Z"
    with database.engine.begin() as connection:
        connection.execute(text("INSERT INTO users (id, discord_user_id, activitypub_username, actor_url, inbox_url, outbox_url, followers_url, public_key_pem, private_key_pem, profile_version, created_at, updated_at) VALUES (1, '1', 'alice', 'https://bridge.example/actors/alice', 'https://bridge.example/actors/alice/inbox', 'https://bridge.example/actors/alice/outbox', 'https://bridge.example/actors/alice/followers', 'public', 'private', 1, :now, :now)"), {"now": now})
        connection.execute(text("INSERT INTO community_thread_groups (id, community_actor_id, source_channel_id, source_thread_id, source_starter_message_id, ap_activity_id, ap_object_id, created_at) VALUES (1, 'https://remote.example/c/python', 100, 200, 201, 'https://bridge.example/users/alice/activities/create/post/create-1', 'https://bridge.example/users/alice/post/thread-1', :now)"), {"now": now})
        connection.execute(text("INSERT INTO community_thread_group_deliveries (id, thread_group_id, discord_channel_id, discord_thread_id, discord_starter_message_id, role) VALUES (1, 1, 100, 200, 201, 'source')"))
        connection.execute(text("INSERT INTO community_message_groups (id, community_actor_id, thread_group_id, source_channel_id, source_thread_id, source_message_id, ap_activity_id, ap_object_id, parent_message_group_id, created_at) VALUES (2, 'https://remote.example/c/python', 1, 100, 200, 300, 'https://bridge.example/users/alice/activities/create/comment/create-msg-1', 'https://bridge.example/users/alice/comment/msg-1', NULL, :now)"), {"now": now})
        connection.execute(text("INSERT INTO community_message_group_deliveries (id, message_group_id, discord_channel_id, discord_thread_id, discord_message_id, role) VALUES (2, 2, 100, 200, 300, 'source')"))

    config = Stage4IdentityMigrationConfig(public_origin="https://bridge.example", service_actor_identifier="bridge")
    database.migrate(config)
    database.migrate(config)

    with database.engine.connect() as connection:
        thread = connection.execute(text("SELECT community_actor_id, ap_activity_id, ap_object_id FROM community_thread_groups WHERE id=1")).mappings().one()
        assert thread["community_actor_id"] == "https://remote.example/c/python"
        assert thread["ap_object_id"] == "https://bridge.example/actors/users/alice/objects/posts/thread-1"
        assert thread["ap_activity_id"] == "https://bridge.example/actors/users/alice/activities/create/posts/create-1"
        message = connection.execute(text("SELECT community_actor_id, ap_activity_id, ap_object_id, parent_message_group_id FROM community_message_groups WHERE id=2")).mappings().one()
        assert message["community_actor_id"] == "https://remote.example/c/python"
        assert message["ap_object_id"] == "https://bridge.example/actors/users/alice/objects/comments/msg-1"
        assert message["ap_activity_id"] == "https://bridge.example/actors/users/alice/activities/create/comments/create-msg-1"
        assert message["parent_message_group_id"] is None
        assert connection.execute(text("SELECT COUNT(*) FROM community_thread_group_deliveries WHERE thread_group_id=1")).scalar_one() == 1
        assert connection.execute(text("SELECT COUNT(*) FROM community_message_group_deliveries WHERE message_group_id=2")).scalar_one() == 1
        assert connection.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    database.close()


def test_stage4_migration_rewrites_community_sync_group_local_community_actor_ids(tmp_path) -> None:
    """Startup migration rewrites same-origin group community actor IDs."""
    database = Database(f"sqlite:///{tmp_path / 'community-sync-actors.db'}")
    database.create_all()
    now = "2026-06-21T12:00:00Z"
    with database.engine.begin() as connection:
        connection.execute(text("INSERT INTO community_thread_groups (id, community_actor_id, source_channel_id, source_thread_id, source_starter_message_id, ap_activity_id, ap_object_id, created_at) VALUES (1, 'https://bridge.example/communities/python', 100, 200, 201, NULL, NULL, :now)"), {"now": now})
        connection.execute(text("INSERT INTO community_message_groups (id, community_actor_id, thread_group_id, source_channel_id, source_thread_id, source_message_id, ap_activity_id, ap_object_id, parent_message_group_id, created_at) VALUES (2, 'https://bridge.example/communities/python', 1, 100, 200, 300, NULL, NULL, NULL, :now)"), {"now": now})

    config = Stage4IdentityMigrationConfig(public_origin="https://bridge.example", service_actor_identifier="bridge")
    database.migrate(config)
    database.migrate(config)

    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT community_actor_id FROM community_thread_groups WHERE id=1")).scalar_one() == "https://bridge.example/actors/communities/python"
        assert connection.execute(text("SELECT community_actor_id FROM community_message_groups WHERE id=2")).scalar_one() == "https://bridge.example/actors/communities/python"
    database.close()



def test_stage4_migration_invariants_cover_community_sync_group_tables(tmp_path) -> None:
    """Startup fails when a group table keeps an unclassified retired local URL."""
    database = Database(f"sqlite:///{tmp_path / 'community-sync-invariant.db'}")
    database.create_all()
    now = "2026-06-21T12:00:00Z"
    with database.engine.begin() as connection:
        connection.execute(text("INSERT INTO community_thread_groups (id, community_actor_id, source_channel_id, source_thread_id, source_starter_message_id, ap_activity_id, ap_object_id, created_at) VALUES (1, 'https://remote.example/c/python', 100, 200, 201, 'https://bridge.example/users/alice/unclassified/create-1', NULL, :now)"), {"now": now})

    config = Stage4IdentityMigrationConfig(public_origin="https://bridge.example", service_actor_identifier="bridge")
    try:
        database.migrate(config)
    except RuntimeError as error:
        assert "community_thread_groups.ap_activity_id" in str(error)
    else:  # pragma: no cover - defensive because the error is the contract.
        raise AssertionError("retired group-table local URL should fail invariant checks")
    finally:
        database.close()

def test_stage4_migration_preserves_remote_community_sync_group_ids(tmp_path) -> None:
    """Startup migration must not rewrite remote AP identity that resembles old paths."""
    database = Database(f"sqlite:///{tmp_path / 'community-sync-remote.db'}")
    database.create_all()
    now = "2026-06-21T12:00:00Z"
    with database.engine.begin() as connection:
        connection.execute(text("INSERT INTO community_thread_groups (id, community_actor_id, source_channel_id, source_thread_id, source_starter_message_id, ap_activity_id, ap_object_id, created_at) VALUES (1, 'https://remote.example/c/python', 100, 200, 201, 'https://remote.example/users/alice/activities/create/post/create-remote', 'https://remote.example/users/alice/post/thread-remote', :now)"), {"now": now})
        connection.execute(text("INSERT INTO community_message_groups (id, community_actor_id, thread_group_id, source_channel_id, source_thread_id, source_message_id, ap_activity_id, ap_object_id, parent_message_group_id, created_at) VALUES (2, 'https://remote.example/c/python', 1, 100, 200, 300, 'https://remote.example/users/alice/activities/create/comment/create-remote', 'https://remote.example/users/alice/comment/msg-remote', NULL, :now)"), {"now": now})

    config = Stage4IdentityMigrationConfig(public_origin="https://bridge.example", service_actor_identifier="bridge")
    database.migrate(config)

    with database.engine.connect() as connection:
        thread = connection.execute(text("SELECT community_actor_id, ap_activity_id, ap_object_id FROM community_thread_groups WHERE id=1")).mappings().one()
        assert dict(thread) == {
            "community_actor_id": "https://remote.example/c/python",
            "ap_activity_id": "https://remote.example/users/alice/activities/create/post/create-remote",
            "ap_object_id": "https://remote.example/users/alice/post/thread-remote",
        }
        message = connection.execute(text("SELECT community_actor_id, ap_activity_id, ap_object_id FROM community_message_groups WHERE id=2")).mappings().one()
        assert dict(message) == {
            "community_actor_id": "https://remote.example/c/python",
            "ap_activity_id": "https://remote.example/users/alice/activities/create/comment/create-remote",
            "ap_object_id": "https://remote.example/users/alice/comment/msg-remote",
        }
    database.close()


def test_stage4_migration_falls_back_to_object_id_for_generic_legacy_activity_tail(
    tmp_path,
) -> None:
    """Stage 4 must keep generic ``/activity`` tails from collapsing into duplicates."""
    database = Database(f"sqlite:///{tmp_path / 'generic-tail.db'}")
    database.create_all()
    now = "2026-06-21T12:00:00Z"
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO local_communities (id, discord_guild_id, discord_forum_channel_id, slug, display_name, actor_url, inbox_url, outbox_url, followers_url, public_key_pem, private_key_pem, status, created_at, updated_at) VALUES (1, 10, 20, 'great_community', 'Great Community', 'https://bot-test.nachitima.com/communities/great_community', 'https://bot-test.nachitima.com/communities/great_community/inbox', 'https://bot-test.nachitima.com/communities/great_community/outbox', 'https://bot-test.nachitima.com/communities/great_community/followers', 'public', 'private', 'active', :now, :now)"
            ),
            {"now": now},
        )
        connection.execute(
                text(
                    "INSERT INTO message_mappings (id, source_platform, source_id, publication_context, activity_id, object_id, actor_url, community_actor_url, discord_channel_id, discord_message_id, created_at) VALUES "
                    "(1, 'discord', '1', 'community', 'https://mastodon.social/ap/users/alice/statuses/1/activity', 'https://mastodon.social/ap/users/alice/statuses/1', 'https://mastodon.social/ap/users/alice', 'https://bot-test.nachitima.com/communities/great_community', 11, 101, :now), "
                    "(2, 'discord', '2', 'community', 'https://mastodon.social/ap/users/alice/statuses/2/activity', 'https://mastodon.social/ap/users/alice/statuses/2', 'https://mastodon.social/ap/users/alice', 'https://bot-test.nachitima.com/communities/great_community', 11, 102, :now)"
                ),
                {"now": now},
            )
        connection.execute(
            text(
                "INSERT INTO local_community_threads (id, local_community_id, ap_activity_id, ap_object_id, direction, origin_kind, created_at) VALUES (1, 1, 'https://bot-test.nachitima.com/users/choikak/activities/create/post/thread-1', 'https://bot-test.nachitima.com/users/choikak/post/thread-1', 'outbound', 'discord', :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO local_community_thread_surfaces (id, local_community_thread_id, discord_forum_channel_id, discord_thread_id, discord_starter_message_id, local_subscriber_id, role, created_at) VALUES (1, 1, 20, 200, 201, NULL, 'host', :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO local_community_messages (id, local_community_thread_id, ap_activity_id, ap_object_id, parent_ap_object_id, direction, created_at) VALUES "
                "(1, 1, 'https://mastodon.social/ap/users/alice/statuses/3/activity', 'https://mastodon.social/ap/users/alice/statuses/3', 'https://bot-test.nachitima.com/users/choikak/post/thread-1', 'outbound', :now), "
                "(2, 1, 'https://mastodon.social/ap/users/alice/statuses/4/activity', 'https://mastodon.social/ap/users/alice/statuses/4', 'https://bot-test.nachitima.com/users/choikak/post/thread-1', 'outbound', :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO local_community_message_surfaces (id, local_community_message_id, local_community_thread_surface_id, discord_forum_channel_id, discord_message_id, parent_discord_message_id, local_subscriber_id, role, created_at) VALUES "
                "(1, 1, 1, 20, 300, 201, NULL, 'host', :now), "
                "(2, 2, 1, 20, 301, 201, NULL, 'host', :now)"
            ),
            {"now": now},
        )

    config = Stage4IdentityMigrationConfig(
        public_origin="https://bot-test.nachitima.com",
        service_actor_identifier="bridge",
    )
    database.migrate(config)
    database.migrate(config)

    with database.engine.connect() as connection:
        mapping_rows = connection.execute(
            text("SELECT id, activity_id, object_id FROM message_mappings ORDER BY id")
        ).mappings().all()
        assert mapping_rows[0]["activity_id"].endswith("/1")
        assert mapping_rows[1]["activity_id"].endswith("/2")
        assert mapping_rows[0]["activity_id"] != mapping_rows[1]["activity_id"]
        message_rows = connection.execute(
            text(
                "SELECT id, ap_activity_id, ap_object_id FROM local_community_messages ORDER BY id"
            )
        ).mappings().all()
        assert message_rows[0]["ap_activity_id"].endswith("/3")
        assert message_rows[1]["ap_activity_id"].endswith("/4")
        assert message_rows[0]["ap_activity_id"] != message_rows[1]["ap_activity_id"]
    database.close()


def test_stage4_migration_rewrites_old_user_actor_urls_for_remote_communities(
    tmp_path,
) -> None:
    """Stage 4 must rewrite old user actor URLs even when the community is remote."""
    database = Database(f"sqlite:///{tmp_path / 'remote-community-actor.db'}")
    database.create_all()
    now = "2026-06-21T12:00:00Z"
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO published_activity_objects (id, actor_username, actor_url, publication_context, community_actor_url, activity_id, object_id, kind, title, body_markdown, in_reply_to_object_id, conversation_root_object_id, discord_guild_id, discord_channel_id, discord_message_id, content_revision, desired_kind, discord_reconciliation_needed, discord_reconciliation_attempt_count, published_at, created_at) VALUES "
                "(1, 'alice', 'https://bot-test.nachitima.com/actors/alice', 'community', 'https://lemmy.world/c/fediverse', 'https://bot-test.nachitima.com/users/alice/activities/create/post/31', 'https://bot-test.nachitima.com/users/alice/post/31', 'post', 'old', 'body', NULL, 'https://bot-test.nachitima.com/users/alice/post/31', 10, 20, 30, 1, 'update', 0, 0, :now, :now)"
            ),
            {"now": now},
        )

    config = Stage4IdentityMigrationConfig(
        public_origin="https://bot-test.nachitima.com",
        service_actor_identifier="bridge",
    )
    database.migrate(config)
    database.migrate(config)

    with database.engine.connect() as connection:
        row = connection.execute(
            text("SELECT actor_url, community_actor_url, activity_id, object_id FROM published_activity_objects WHERE id=1")
        ).mappings().one()
        assert row["actor_url"] == "https://bot-test.nachitima.com/actors/users/alice"
        assert row["community_actor_url"] == "https://lemmy.world/c/fediverse"
        assert row["activity_id"] == "https://bot-test.nachitima.com/actors/users/alice/activities/create/posts/31"
        assert row["object_id"] == "https://bot-test.nachitima.com/actors/users/alice/objects/posts/31"
    database.close()


def test_stage4_migration_rewrites_discord_to_ap_local_community_rows(tmp_path) -> None:
    """Stage 4 must migrate old Discord-origin local community rows in both directions."""
    database = Database(f"sqlite:///{tmp_path / 'discord-to-ap-local-community.db'}")
    database.create_all()
    now = "2026-06-21T12:00:00Z"
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO local_communities (id, discord_guild_id, discord_forum_channel_id, slug, display_name, actor_url, inbox_url, outbox_url, followers_url, public_key_pem, private_key_pem, status, created_at, updated_at) VALUES (1, 10, 20, 'great_community', 'Great Community', 'https://bot-test.nachitima.com/communities/great_community', 'https://bot-test.nachitima.com/communities/great_community/inbox', 'https://bot-test.nachitima.com/communities/great_community/outbox', 'https://bot-test.nachitima.com/communities/great_community/followers', 'public', 'private', 'active', :now, :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO local_community_threads (id, local_community_id, ap_activity_id, ap_object_id, direction, origin_kind, created_at) VALUES (1, 1, 'https://bot-test.nachitima.com/users/choikak/activities/create/post/31', 'https://bot-test.nachitima.com/users/choikak/post/31', 'discord_to_ap', 'discord', :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO local_community_thread_surfaces (id, local_community_thread_id, discord_forum_channel_id, discord_thread_id, discord_starter_message_id, local_subscriber_id, role, created_at) VALUES (1, 1, 20, 200, 201, NULL, 'host', :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO local_community_messages (id, local_community_thread_id, ap_activity_id, ap_object_id, parent_ap_object_id, direction, created_at) VALUES (1, 1, 'https://bot-test.nachitima.com/users/choikak/activities/create/comment/32', 'https://bot-test.nachitima.com/users/choikak/comment/32', 'https://bot-test.nachitima.com/users/choikak/post/31', 'discord_to_ap', :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO local_community_message_surfaces (id, local_community_message_id, local_community_thread_surface_id, discord_forum_channel_id, discord_message_id, parent_discord_message_id, local_subscriber_id, role, created_at) VALUES (1, 1, 1, 20, 300, 201, NULL, 'host', :now)"
            ),
            {"now": now},
        )

    config = Stage4IdentityMigrationConfig(
        public_origin="https://bot-test.nachitima.com",
        service_actor_identifier="bridge",
    )
    database.migrate(config)
    database.migrate(config)

    with database.engine.connect() as connection:
        thread = connection.execute(
            text("SELECT ap_activity_id, ap_object_id FROM local_community_threads WHERE id=1")
        ).mappings().one()
        message = connection.execute(
            text("SELECT ap_activity_id, ap_object_id, parent_ap_object_id FROM local_community_messages WHERE id=1")
        ).mappings().one()
        assert thread["ap_activity_id"] == "https://bot-test.nachitima.com/actors/communities/great_community/activities/create/posts/31"
        assert thread["ap_object_id"] == "https://bot-test.nachitima.com/actors/communities/great_community/objects/posts/31"
        assert message["ap_activity_id"] == "https://bot-test.nachitima.com/actors/communities/great_community/activities/create/comments/32"
        assert message["ap_object_id"] == "https://bot-test.nachitima.com/actors/communities/great_community/objects/comments/32"
        assert message["parent_ap_object_id"] == thread["ap_object_id"]
    database.close()


def test_stage4_migration_rewrites_message_mapping_actor_urls_for_remote_communities(
    tmp_path,
) -> None:
    """Stage 4 must rewrite message-mapping actor URLs even when the community is remote."""
    database = Database(f"sqlite:///{tmp_path / 'remote-community-message-mapping.db'}")
    database.create_all()
    now = "2026-06-21T12:00:00Z"
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO message_mappings (id, source_platform, source_id, publication_context, activity_id, object_id, actor_url, community_actor_url, discord_channel_id, discord_message_id, created_at) VALUES "
                "(1, 'discord', '1', 'community', 'https://bot-test.nachitima.com/users/alice/activities/create/comment/31', 'https://bot-test.nachitima.com/users/alice/comment/31', 'https://bot-test.nachitima.com/actors/alice', 'https://lemmy.world/c/fediverse', 10, 20, :now)"
            ),
            {"now": now},
        )

    config = Stage4IdentityMigrationConfig(
        public_origin="https://bot-test.nachitima.com",
        service_actor_identifier="bridge",
    )
    database.migrate(config)
    database.migrate(config)

    with database.engine.connect() as connection:
        row = connection.execute(
            text("SELECT actor_url, community_actor_url, activity_id, object_id FROM message_mappings WHERE id=1")
        ).mappings().one()
        assert row["actor_url"] == "https://bot-test.nachitima.com/actors/users/alice"
        assert row["community_actor_url"] == "https://lemmy.world/c/fediverse"
        assert row["activity_id"] == "https://bot-test.nachitima.com/actors/users/alice/activities/create/comments/31"
        assert row["object_id"] == "https://bot-test.nachitima.com/actors/users/alice/objects/comments/31"
    database.close()
