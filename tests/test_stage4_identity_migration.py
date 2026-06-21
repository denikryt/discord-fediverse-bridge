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
