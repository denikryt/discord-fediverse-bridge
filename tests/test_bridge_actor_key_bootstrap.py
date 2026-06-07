"""Bridge actor key bootstrap and access-service contracts."""

from __future__ import annotations

import json

import pytest

from src.actor_key_service import ActorKeyService, BridgeActorKeyBootstrap
from src.config import Settings
from src.db import Database


def _settings(**overrides: object) -> Settings:
    """Build settings without requiring deployment secrets unrelated to this test."""
    values = {
        "DISCORD_TOKEN": "token",
        "FEDIFY_SHARED_SECRET": "secret",
        "FEDIFY_ORIGIN": "https://bridge.example",
        "FEDIFY_ACTOR_IDENTIFIER": "bridge",
    }
    values.update(overrides)
    return Settings(**values)


def test_bootstrap_imports_legacy_jwk_once(tmp_path) -> None:
    """An existing deployment imports its exact JWK text and never overwrites it."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    private = json.dumps({"kty": "RSA", "d": "private"})
    public = json.dumps({"kty": "RSA", "n": "public"})
    imported = BridgeActorKeyBootstrap(
        database=database,
        settings=_settings(
            FEDIFY_BRIDGE_PRIVATE_KEY_JWK_JSON=private,
            FEDIFY_BRIDGE_PUBLIC_KEY_JWK_JSON=public,
        ),
    ).ensure()
    reused = BridgeActorKeyBootstrap(
        database=database,
        settings=_settings(
            FEDIFY_BRIDGE_PRIVATE_KEY_JWK_JSON=json.dumps({"kty": "RSA", "d": "other"}),
            FEDIFY_BRIDGE_PUBLIC_KEY_JWK_JSON=json.dumps({"kty": "RSA", "n": "other"}),
        ),
    ).ensure()
    assert reused.id == imported.id
    assert reused.private_key_data == private
    assert reused.public_key_data == public


def test_bootstrap_rejects_partial_or_invalid_legacy_pair(tmp_path) -> None:
    """Partial or malformed compatibility input must not create split identity."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    with pytest.raises(RuntimeError, match="Both legacy"):
        BridgeActorKeyBootstrap(
            database=database,
            settings=_settings(FEDIFY_BRIDGE_PRIVATE_KEY_JWK_JSON="{}"),
        ).ensure()
    with pytest.raises(RuntimeError, match="invalid JSON"):
        BridgeActorKeyBootstrap(
            database=database,
            settings=_settings(
                FEDIFY_BRIDGE_PRIVATE_KEY_JWK_JSON="not-json",
                FEDIFY_BRIDGE_PUBLIC_KEY_JWK_JSON="{}",
            ),
        ).ensure()
    assert database.bridge_actor_keys.get() is None


def test_new_deployment_generates_persistent_pem_key(tmp_path) -> None:
    """A new deployment generates one DB-backed key and reuses it on restart."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    first = BridgeActorKeyBootstrap(database=database, settings=_settings()).ensure()
    second = BridgeActorKeyBootstrap(database=database, settings=_settings()).ensure()
    assert first.id == second.id
    assert first.key_format == "pem"
    assert "BEGIN PUBLIC KEY" in first.public_key_data
    assert "BEGIN PRIVATE KEY" in first.private_key_data
    assert "private_key_data" not in repr(first)


def test_actor_key_service_preserves_owner_specific_storage(tmp_path) -> None:
    """The facade resolves keys without moving user/community domain columns."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    BridgeActorKeyBootstrap(database=database, settings=_settings()).ensure()
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://bridge.example/actors/alice",
        inbox_url="https://bridge.example/actors/alice/inbox",
        outbox_url="https://bridge.example/actors/alice/outbox",
        followers_url="https://bridge.example/actors/alice/followers",
        public_key_pem="user-public",
        private_key_pem="user-private",
    )
    community = database.local_communities.create_local_community(
        discord_guild_id=1,
        discord_forum_channel_id=2,
        slug="community",
        display_name="Community",
        summary=None,
        created_by_discord_user_id="1",
        actor_url="https://bridge.example/actors/community",
        inbox_url="https://bridge.example/actors/community/inbox",
        outbox_url="https://bridge.example/actors/community/outbox",
        followers_url="https://bridge.example/actors/community/followers",
        public_key_pem="community-public",
        private_key_pem="community-private",
    )
    service = ActorKeyService(database)
    assert service.get_bridge_actor_keys().key_format == "pem"
    assert service.get_user_actor_keys(user.id).private_key_data == "user-private"
    assert service.get_local_community_actor_keys(community.id).private_key_data == "community-private"
