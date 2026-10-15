"""Authentic release URL cutover and conditional relationship confirmation reset."""

from pathlib import Path
import pytest
from sqlalchemy import event
from src.db import Database
from test_legacy_v0_to_v1_migration import legacy, _seed, _evidence


def _released_urls(db, *, community_changed=True, service_changed=True):
    """Populate release rows, then use the exact namespaces emitted by the tag."""
    _seed(db)
    with db.engine.begin() as c:
        if community_changed:
            c.exec_driver_sql(
                "UPDATE local_communities SET actor_url='https://bridge.test/communities/news',inbox_url='https://bridge.test/communities/news/inbox',outbox_url='https://bridge.test/communities/news/outbox',followers_url='https://bridge.test/communities/news/followers' WHERE id=8"
            )
        c.exec_driver_sql(
            "UPDATE users SET actor_url='https://bridge.test/actors/alice',inbox_url='https://bridge.test/actors/alice/inbox',outbox_url='https://bridge.test/actors/alice/outbox',followers_url='https://bridge.test/actors/alice/followers' WHERE id=7"
        )
        if service_changed:
            c.exec_driver_sql(
                "UPDATE bridge_actor_keys SET actor_url='https://bridge.test/actors/bridge',key_id='https://bridge.test/actors/bridge#main-key'"
            )
        for i, kind in enumerate(["post", "comment"]):
            c.exec_driver_sql(
                "UPDATE published_activity_objects SET actor_url='https://bridge.test/actors/alice',object_id=?,activity_id=?,in_reply_to_object_id=? WHERE id=?",
                (
                    f"https://bridge.test/users/alice/{kind}/{i}",
                    f"https://bridge.test/users/alice/activities/create/{kind}/{i}",
                    None if i == 0 else "https://bridge.test/users/alice/post/0",
                    20 + i,
                ),
            )
        c.exec_driver_sql(
            "UPDATE message_mappings SET actor_url='https://bridge.test/actors/alice',object_id='https://bridge.test/users/alice/post/22',activity_id='https://bridge.test/users/alice/activities/create/post/22'"
        )
        if community_changed:
            c.exec_driver_sql(
                "UPDATE local_community_threads SET ap_object_id='https://bridge.test/communities/news/post/50',ap_activity_id='https://bridge.test/communities/news/activities/50' WHERE id=50"
            )
            c.exec_driver_sql(
                "UPDATE local_community_messages SET ap_object_id='https://bridge.test/communities/news/comment/51',ap_activity_id='https://bridge.test/communities/news/activities/51',parent_ap_object_id='https://bridge.test/communities/news/post/50' WHERE id=51"
            )


def _config():
    """Supply deployment's explicit non-secret canonical identity context."""
    from src.db.legacy_identity_cutover import IdentityCutoverConfig

    return IdentityCutoverConfig("https://bridge.test", "bridge")


@pytest.mark.parametrize("community_changed", [False, True])
@pytest.mark.parametrize("service_changed", [False, True])
def test_cutover_resets_only_affected_relationships(
    legacy, community_changed, service_changed
):
    """One upgrade preserves keys/content/channels and resets only changed actor relations."""
    _released_urls(
        legacy, community_changed=community_changed, service_changed=service_changed
    )
    before = _evidence(legacy)
    legacy.migrate(identity=_config())
    after = _evidence(legacy)
    assert after[0] == 1
    for table in [
        "local_subscribers",
        "local_community_thread_surfaces",
        "local_community_message_surfaces",
    ]:
        for old, new in zip(before[2][table], after[2][table], strict=True):
            assert all(new[k] == v for k, v in old.items())
    assert len(after[2]["remote_subscribers"]) == (0 if community_changed else 1)
    assert after[2]["users"][0]["actor_url"] == "https://bridge.test/actors/users/alice"
    assert after[2]["bridge_actor_keys"][0]["private_key_data"] == "service-private"
    assert after[2]["local_communities"][0]["private_key_pem"] == "cprivate"
    assert after[2]["users"][0]["private_key_pem"] == "private"
    root = "https://bridge.test/actors/users/alice/objects/posts/0"
    assert after[2]["published_activity_objects"][0]["object_id"] == root
    assert after[2]["published_activity_objects"][1]["in_reply_to_object_id"] == root
    assert (
        after[2]["published_activity_objects"][1]["conversation_root_object_id"] == root
    )
    for table in ["bridge_actor_follows", "channel_community_subscriptions"]:
        for old, new in zip(before[2][table], after[2][table], strict=True):
            if service_changed:
                assert new["status"] == "failed" and new["follow_activity_id"] is None
                assert new["id"] == old["id"] and new["created_at"] == old["created_at"]
            else:
                assert all(new[k] == v for k, v in old.items())
    legacy.migrate(identity=_config())
    assert _evidence(legacy) == after


def test_cutover_requires_context_and_rollback_restores_old_namespace(
    legacy, monkeypatch
):
    """Missing settings or a downstream failure leaves exact old URLs/relationships."""
    _released_urls(legacy)
    before = _evidence(legacy)
    with pytest.raises(RuntimeError, match="identity|origin"):
        legacy.migrate()
    assert _evidence(legacy) == before
    from src.db import migrations

    real = migrations._upgrade_platform_model_from_legacy_input

    def fail(conn):
        """Abort after the identity phase wrote real rows, before target stamping."""
        assert (
            conn.exec_driver_sql("SELECT actor_url FROM users WHERE id=7").scalar_one()
            == "https://bridge.test/actors/users/alice"
        )
        raise RuntimeError("cutover fault")

    monkeypatch.setattr(migrations, "_upgrade_platform_model_from_legacy_input", fail)
    with pytest.raises(RuntimeError, match="cutover fault"):
        legacy.migrate(identity=_config())
    assert _evidence(legacy) == before
    monkeypatch.setattr(migrations, "_upgrade_platform_model_from_legacy_input", real)
    legacy.migrate(identity=_config())


def _relay_graph(db, *, signed=False):
    """Seed a retained remote payload referring to an old local post and followers."""
    import json

    payload = {
        "id": "https://remote.test/activities/old",
        "actor": "https://remote.test/u/bob",
        "object": {
            "id": "https://remote.test/comment/old",
            "inReplyTo": "https://bridge.test/communities/news/post/50",
            "content": "https://bridge.test/communities/news/post/50",
        },
        "cc": ["https://bridge.test/communities/news/followers"],
    }
    if signed:
        payload["signature"] = {"signatureValue": "opaque"}
    with db.engine.begin() as c:
        c.exec_driver_sql(
            "INSERT INTO local_community_relay_source_activities(id,local_community_id,object_kind,operation,source_object_ap_id,source_activity_id,origin_remote_actor_id,source_activity_json,created_at) VALUES(80,8,'comment','create','https://remote.test/comment/old','https://remote.test/activities/old','https://remote.test/u/bob',?,CURRENT_TIMESTAMP)",
            (json.dumps(payload),),
        )
        c.exec_driver_sql(
            "INSERT INTO local_community_relay_deliveries(id,source_activity_row_id,local_community_id,object_kind,operation,source_object_ap_id,source_activity_id,origin_remote_actor_id,target_remote_actor_id,target_inbox_url,delivery_profile,relay_activity_id,status,attempt_count,created_at,updated_at) VALUES(81,80,8,'comment','create','https://remote.test/comment/old','https://remote.test/activities/old','https://remote.test/u/bob','https://remote.test/u/alice','https://remote.test/u/alice/inbox','threadiverse_group','https://bridge.test/communities/news/activities/announce/81','failed',2,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
    return payload


def test_linked_payloads_and_mixed_community_confirmations(legacy):
    """Only renamed community's followers disappear; routing links change, prose/audit stay."""
    import runpy, json
    from sqlalchemy.orm import Session

    _released_urls(legacy)
    payload = _relay_graph(legacy)
    old = runpy.run_path(
        str(Path(__file__).parent / "fixtures/release_v0_1_0_models.py")
    )
    with Session(legacy.engine) as s:
        s.add(
            old["LocalCommunity"](
                id=90,
                discord_guild_id=1,
                discord_forum_channel_id=990,
                slug="stable",
                display_name="Stable",
                actor_url="https://bridge.test/actors/communities/stable",
                inbox_url="https://bridge.test/actors/communities/stable/inbox",
                outbox_url="https://bridge.test/actors/communities/stable/outbox",
                followers_url="https://bridge.test/actors/communities/stable/followers",
                public_key_pem="stable-public",
                private_key_pem="stable-private",
            )
        )
        s.add(
            old["RemoteSubscriber"](
                id=91,
                local_community_id=90,
                remote_actor_id="https://remote.test/u/stable",
                remote_inbox_url="https://remote.test/u/stable/inbox",
                follow_activity_id="https://remote.test/follow/91",
            )
        )
        s.commit()
    before = _evidence(legacy)
    legacy.migrate(identity=_config())
    after = _evidence(legacy)
    assert after[2]["remote_subscribers"] == [before[2]["remote_subscribers"][1]]
    assert after[2]["management_audit_events"] == before[2]["management_audit_events"]
    rewritten = json.loads(
        after[2]["local_community_relay_source_activities"][0]["source_activity_json"]
    )
    assert rewritten["id"] == payload["id"] and rewritten["actor"] == payload["actor"]
    assert rewritten["object"]["content"] == payload["object"]["content"]
    assert (
        rewritten["object"]["inReplyTo"]
        == "https://bridge.test/actors/communities/news/objects/posts/50"
    )
    assert rewritten["cc"] == ["https://bridge.test/actors/communities/news/followers"]
    assert (
        after[2]["local_community_relay_deliveries"][0]["relay_activity_id"]
        == "https://bridge.test/actors/communities/news/activities/announce/comments/81"
    )


@pytest.mark.parametrize("defect", ["signed", "collision", "origin", "owner"])
def test_unprovable_cutover_is_rejected_without_changes(legacy, defect):
    """Migration cannot forge signatures, collapse IDs, move domains or invent local owners."""
    _released_urls(legacy)
    if defect == "signed":
        _relay_graph(legacy, signed=True)
    else:
        with legacy.engine.begin() as c:
            if defect == "collision":
                c.exec_driver_sql(
                    "UPDATE published_activity_objects SET object_id='https://bridge.test/actors/users/alice/objects/posts/0',kind='post' WHERE id=21"
                )
            elif defect == "origin":
                c.exec_driver_sql(
                    "UPDATE users SET actor_url='https://elsewhere.test/actors/alice' WHERE id=7"
                )
            else:
                c.exec_driver_sql(
                    "UPDATE message_mappings SET object_id='https://bridge.test/users/unknown/post/22'"
                )
    before = _evidence(legacy)
    with pytest.raises(RuntimeError):
        legacy.migrate(identity=_config())
    assert _evidence(legacy) == before


@pytest.mark.asyncio
async def test_reset_subscription_can_retry_without_replacing_claim(legacy):
    """The existing moderator subscribe operation retries the retained failed owner in place."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from discordops import run_operation_definition_async
    from src.operations import SubscribeInput, subscribe_operation
    from support.runtime import build_test_policy_service
    from src.fedify_gateway_client import FollowCommunityResult

    _released_urls(legacy)
    legacy.migrate(identity=_config())
    before = legacy.remote_subscriptions.get_subscription_by_channel(902)
    gateway = AsyncMock()
    gateway.follow_community.return_value = FollowCommunityResult(
        community_actor_url="https://remote.test/c/news",
        community_inbox_url="https://remote.test/c/news/inbox",
        follow_activity_id="https://bridge.test/actors/services/bridge/activities/follow/new",
    )
    result = await run_operation_definition_async(
        subscribe_operation,
        SubscribeInput(
            database=legacy,
            fedify_gateway=gateway,
            discord_user_id="1",
            channel_id=902,
            channel_mention="<#902>",
            actor_id="https://remote.test/c/news",
            community_name="news",
            numeric_id=None,
            community_handle="!news@remote.test",
            policy_service=build_test_policy_service(legacy),
        ),
    )
    assert result.applied
    after = legacy.remote_subscriptions.get_subscription_by_channel(902)
    assert (
        after.id == before.id
        and after.created_at == before.created_at
        and after.status == "pending"
    )
    assert after.follow_activity_id.endswith("/follow/new")
    assert (
        legacy.platform_surfaces.resolve_channel(
            "discord-primary", "902"
        ).remote_subscription_id
        == after.id
    )


def test_current_identity_checks_reject_bad_v1_without_second_cutover(legacy):
    """Configured startup never repairs/reset relationships on a stamped incompatible target."""
    _released_urls(legacy)
    legacy.migrate(identity=_config())
    with legacy.engine.begin() as c:
        c.exec_driver_sql(
            "UPDATE users SET actor_url='https://bridge.test/actors/alice' WHERE id=7"
        )
    before = _evidence(legacy)
    with pytest.raises(RuntimeError, match="incompatible actor identity"):
        legacy.migrate(identity=_config())
    assert _evidence(legacy) == before


def test_three_contenders_cutover_once(legacy):
    """Concurrent migration resets only during the one locked version transition."""
    from concurrent.futures import ThreadPoolExecutor

    _released_urls(legacy)
    with ThreadPoolExecutor(3) as pool:
        list(pool.map(lambda _: legacy.migrate(identity=_config()), range(3)))
    data = _evidence(legacy)
    assert data[0] == 1 and len(data[2]["local_subscribers"]) == 1
    assert (
        len(data[2]["platform_connectors"]) == 1 and data[2]["remote_subscribers"] == []
    )
    assert all(
        r["status"] == "failed" for r in data[2]["channel_community_subscriptions"]
    )


def test_cutover_actor_api_emits_typed_identity_and_original_key(legacy):
    """The real gateway read API exposes converted actor IDs and unchanged signing material."""
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from src.http_api import create_http_app
    from support.runtime import build_test_policy_service

    _released_urls(legacy)
    legacy.migrate(identity=_config())
    settings = SimpleNamespace(
        fedify_shared_secret="secret",
        normalized_fedify_origin="https://bridge.test",
        normalized_public_bridge_base_url="https://bridge.test",
        fedify_origin="https://bridge.test",
        fedify_actor_identifier="bridge",
        federation_allowlist=[],
        registration_session_cookie_name="session",
        registration_session_ttl_seconds=3600,
    )
    runtime = SimpleNamespace(
        settings=settings,
        database=legacy,
        registration_service=SimpleNamespace(),
        discord_oauth_client=SimpleNamespace(),
        fedify_gateway=SimpleNamespace(),
        bot=SimpleNamespace(),
        bridge_policy_service=build_test_policy_service(legacy, settings),
    )
    with TestClient(create_http_app(runtime)) as client:
        for route, actor, key in [
            (
                "/internal/fedify/actors/users/alice",
                "https://bridge.test/actors/users/alice",
                "private",
            ),
            (
                "/internal/fedify/actors/communities/news",
                "https://bridge.test/actors/communities/news",
                "cprivate",
            ),
        ]:
            response = client.get(route, headers={"Authorization": "Bearer secret"})
            assert (
                response.status_code == 200
                and response.json()["actor_url"] == actor
                and response.json()["private_key_pem"] == key
            )
        bridge = client.get(
            "/internal/fedify/actors/bridge/key",
            headers={"Authorization": "Bearer secret"},
        )
        assert (
            bridge.status_code == 200
            and bridge.json()["actor_url"]
            == "https://bridge.test/actors/services/bridge"
        )
        assert (
            bridge.json()["key_id"]
            == "https://bridge.test/actors/services/bridge#main-key"
            and bridge.json()["private_key_data"] == "service-private"
        )


def test_service_target_identifier_comes_from_explicit_settings(legacy):
    """A configured service name change preserves its singleton key and resets old confirmation."""
    from src.db.legacy_identity_cutover import IdentityCutoverConfig

    _released_urls(legacy)
    config = IdentityCutoverConfig("https://bridge.test", "configured-service")
    legacy.migrate(identity=config)
    data = _evidence(legacy)
    assert (
        data[2]["bridge_actor_keys"][0]["actor_url"]
        == "https://bridge.test/actors/services/configured-service"
    )
    assert data[2]["bridge_actor_keys"][0]["private_key_data"] == "service-private"
    assert all(
        r["status"] == "failed" for r in data[2]["channel_community_subscriptions"]
    )
    legacy.migrate(identity=config)
    assert _evidence(legacy) == data


@pytest.mark.parametrize(
    'table,column,value',
    [
        ('local_communities', 'inbox_url', 'https://elsewhere.example/inbox'),
        ('bridge_actor_keys', 'key_id', 'https://elsewhere.example/keys/random'),
        ('users', 'outbox_url', 'https://bridge.test/actors/bob/outbox'),
        ('local_communities', 'followers_url', 'https://bridge.test/communities/other/followers'),
    ],
)
def test_conflicting_source_actor_metadata_is_rejected(legacy, table, column, value):
    """Startup refuses foreign or another actor's metadata without rewriting the database."""
    _released_urls(legacy)
    with legacy.engine.begin() as c:
        c.exec_driver_sql(f'UPDATE {table} SET {column}=?', (value,))
    before = _evidence(legacy)
    with pytest.raises(RuntimeError, match='actor metadata'):
        legacy.migrate(identity=_config())
    assert _evidence(legacy) == before


@pytest.mark.parametrize(
    'column,value',
    [
        ('object_id', 'https://bridge.test/completely-unknown-legacy-format/0'),
        ('activity_id', 'https://bridge.test/completely-unknown-legacy-format/0'),
        ('object_id', 'https://bridge.test/actors/users/alice/objects/unknown/0'),
        ('activity_id', 'https://bridge.test/actors/users/alice/activities/unknown/posts/0'),
        ('object_id', 'https://bridge.test/actors/users/nobody/objects/posts/0'),
        ('object_id', 'https://bridge.test/actors/services/bridge/objects/posts/0'),
        ('activity_id', 'https://bridge.test/actors/users/alice/activities/announce/posts/0'),
        ('object_id', 'https://bridge.test/actors/alice'),
        ('object_id', 'https://bridge.test/users/alice/activities/create/post/0'),
        ('object_id', 'https://bridge.test/users/alice/post/0/'),
        ('activity_id', 'https://bridge.test/users/alice/activities/unknown/extra/0'),
    ],
)
def test_unknown_local_canonical_identity_is_rejected(legacy, column, value):
    """Startup cannot stamp v1 while preserving an unrecognized bridge-owned canonical ID."""
    _released_urls(legacy)
    with legacy.engine.begin() as c:
        c.exec_driver_sql(f'UPDATE published_activity_objects SET {column}=? WHERE id=20', (value,))
    before = _evidence(legacy)
    with pytest.raises(RuntimeError, match='local content identity|local legacy owner|legacy local identity'):
        legacy.migrate(identity=_config())
    assert _evidence(legacy) == before


def test_arbitrary_remote_canonical_identity_is_preserved(legacy):
    """Unknown remote namespaces remain remote data and do not trigger local validation."""
    _released_urls(legacy)
    value = 'https://elsewhere.example/completely-unknown-remote-format/0'
    with legacy.engine.begin() as c:
        c.exec_driver_sql('UPDATE published_activity_objects SET object_id=? WHERE id=20', (value,))
    legacy.migrate(identity=_config())
    with legacy.engine.connect() as c:
        assert c.exec_driver_sql('SELECT object_id FROM published_activity_objects WHERE id=20').scalar_one() == value


def test_known_source_metadata_forms_are_accepted(legacy):
    """Both historical and typed collections of the same registered source actor are valid."""
    _released_urls(legacy)
    with legacy.engine.begin() as c:
        c.exec_driver_sql("UPDATE users SET inbox_url='https://bridge.test/actors/users/alice/inbox'")
        c.exec_driver_sql("UPDATE bridge_actor_keys SET key_id='https://bridge.test/actors/services/bridge#main-key'")
    legacy.migrate(identity=_config())
    with legacy.engine.connect() as c:
        assert c.exec_driver_sql('SELECT inbox_url FROM users').scalar_one() == 'https://bridge.test/actors/users/alice/inbox'


def test_unknown_local_relay_activity_is_rejected(legacy):
    """An outgoing relay's canonical activity is local even though its source object is remote."""
    _released_urls(legacy)
    _relay_graph(legacy)
    with legacy.engine.begin() as c:
        c.exec_driver_sql("UPDATE local_community_relay_deliveries SET relay_activity_id='https://bridge.test/unknown-relay-format/81'")
    before = _evidence(legacy)
    with pytest.raises(RuntimeError, match='local content identity'):
        legacy.migrate(identity=_config())
    assert _evidence(legacy) == before


def _other_local_owners(db):
    """Register genuine alternate owners so existence alone cannot reject a cross-owner ID."""
    with db.engine.begin() as c:
        c.exec_driver_sql(
            "INSERT INTO users(id,discord_user_id,activitypub_username,actor_url,inbox_url,outbox_url,followers_url,public_key_pem,private_key_pem,created_at,updated_at) SELECT 17,'17','bob','https://bridge.test/actors/bob','https://bridge.test/actors/bob/inbox','https://bridge.test/actors/bob/outbox','https://bridge.test/actors/bob/followers',public_key_pem,private_key_pem,created_at,updated_at FROM users WHERE id=7"
        )
        c.exec_driver_sql(
            "INSERT INTO local_communities(id,discord_guild_id,discord_forum_channel_id,slug,display_name,actor_url,inbox_url,outbox_url,followers_url,public_key_pem,private_key_pem,created_at,updated_at,status) SELECT 18,discord_guild_id,918,'other','Other','https://bridge.test/actors/communities/other','https://bridge.test/actors/communities/other/inbox','https://bridge.test/actors/communities/other/outbox','https://bridge.test/actors/communities/other/followers',public_key_pem,private_key_pem,created_at,updated_at,status FROM local_communities WHERE id=8"
        )


@pytest.mark.parametrize('typed', [False, True])
@pytest.mark.parametrize('table,column,row_id', [
    ('published_activity_objects', 'object_id', 20),
    ('published_activity_objects', 'activity_id', 20),
    ('message_mappings', 'object_id', 22),
    ('message_mappings', 'activity_id', 22),
    ('local_community_relay_deliveries', 'relay_activity_id', 81),
])
def test_cross_owner_canonical_id_is_rejected(legacy, typed, table, column, row_id):
    """Startup rejects IDs of another registered actor with exact source rollback."""
    _released_urls(legacy)
    _other_local_owners(legacy)
    if table == 'local_community_relay_deliveries':
        _relay_graph(legacy)
        path = (
            '/actors/communities/other/activities/announce/comments/81'
            if typed else '/communities/other/activities/announce/81'
        )
    elif column == 'object_id':
        path = '/actors/users/bob/objects/posts/0' if typed else '/users/bob/post/0'
    else:
        path = '/actors/users/bob/activities/create/posts/0' if typed else '/users/bob/activities/create/post/0'
    with legacy.engine.begin() as c:
        c.exec_driver_sql(f'UPDATE {table} SET {column}=? WHERE id=?', ('https://bridge.test' + path, row_id))
    before = _evidence(legacy)
    with pytest.raises(RuntimeError, match='content owner mismatch'):
        legacy.migrate(identity=_config())
    assert _evidence(legacy) == before


def test_inbound_mapping_and_community_context_do_not_determine_content_owner(legacy):
    """Incoming relay actors and destination communities are not the original object author."""
    _released_urls(legacy)
    _other_local_owners(legacy)
    with legacy.engine.begin() as c:
        c.exec_driver_sql("UPDATE message_mappings SET source_platform='activitypub',source_id='https://bridge.test/users/alice/post/22',actor_url='https://remote.test/u/relay',activity_id='https://remote.test/arbitrary/announce/22'")
        c.exec_driver_sql("UPDATE community_thread_groups SET ap_object_id='https://bridge.test/users/alice/post/50',ap_activity_id='https://bridge.test/users/alice/activities/create/post/50' WHERE id=40")
        c.exec_driver_sql("UPDATE local_community_threads SET ap_object_id='https://bridge.test/users/alice/post/50',ap_activity_id='https://bridge.test/users/alice/activities/create/post/50' WHERE id=50")
    legacy.migrate(identity=_config())
    rows = _evidence(legacy)[2]
    assert rows['message_mappings'][0]['actor_url'] == 'https://remote.test/u/relay'
    assert rows['message_mappings'][0]['activity_id'] == 'https://remote.test/arbitrary/announce/22'
    assert rows['message_mappings'][0]['object_id'] == 'https://bridge.test/actors/users/alice/objects/posts/22'
    assert rows['local_community_threads'][0]['ap_object_id'] == 'https://bridge.test/actors/users/alice/objects/posts/50'
    assert rows['community_thread_groups'][0]['ap_object_id'] == rows['local_community_threads'][0]['ap_object_id']
