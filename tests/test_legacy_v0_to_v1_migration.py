"""Released SQLite upgrade preserves identities through one atomic public transition."""

import shutil
import runpy
import sqlite3
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session
from src.db import Database, migrations
from src.db import legacy_v0_to_v1 as direct


@pytest.fixture(params=["tagged_database", "independent_models"])
def legacy(tmp_path, request):
    """Open the independently pinned released DDL with FK-enforcing connections."""
    path = tmp_path / "release.db"
    db = Database(f"sqlite:///{path}")
    if request.param == "tagged_database":
        shutil.copy2(
            Path(__file__).parent / "fixtures/release_v0_1_0_original.sqlite", path
        )
    else:
        old = runpy.run_path(
            str(Path(__file__).parent / "fixtures/release_v0_1_0_models.py")
        )
        old["Base"].metadata.create_all(db.engine)
    yield db
    db.close()


def _seed(db):
    """Install authored root/reply, routing state and signing identities from old ORM."""
    old = runpy.run_path(
        str(Path(__file__).parent / "fixtures/release_v0_1_0_models.py")
    )
    with Session(db.engine) as s:
        s.add(
            old["User"](
                id=7,
                discord_user_id="1",
                activitypub_username="alice",
                actor_url="https://bridge.test/actors/users/alice",
                inbox_url="https://bridge.test/actors/users/alice/inbox",
                outbox_url="https://bridge.test/actors/users/alice/outbox",
                followers_url="https://bridge.test/actors/users/alice/followers",
                public_key_pem="public",
                private_key_pem="private",
            )
        )
        s.add(
            old["LocalCommunity"](
                id=8,
                discord_guild_id=1,
                discord_forum_channel_id=900,
                slug="news",
                display_name="News",
                actor_url="https://bridge.test/actors/communities/news",
                inbox_url="https://bridge.test/actors/communities/news/inbox",
                outbox_url="https://bridge.test/actors/communities/news/outbox",
                followers_url="https://bridge.test/actors/communities/news/followers",
                public_key_pem="cpub",
                private_key_pem="cprivate",
            )
        )
        s.add(
            old["LocalSubscriber"](
                id=9, local_community_id=8, discord_guild_id=1, discord_channel_id=901
            )
        )
        for i, status in enumerate(["accepted", "pending", "failed"]):
            s.add(
                old["ChannelCommunitySubscription"](
                    id=10 + i,
                    discord_guild_id=1,
                    discord_channel_id=902 + i,
                    lemmy_community_actor_id="https://remote.test/c/news",
                    status=status,
                )
            )
        s.add(
            old["BridgeActorFollow"](
                id=3,
                community_actor_id="https://remote.test/c/news",
                follow_activity_id="https://bridge.test/follows/3",
                status="accepted",
            )
        )
        for i, kind in enumerate(["post", "comment"]):
            s.add(
                old["PublishedActivityObject"](
                    id=20 + i,
                    actor_username="alice",
                    actor_url="https://bridge.test/actors/users/alice",
                    community_actor_url="https://remote.test/c/news",
                    activity_id=f"https://bridge.test/actors/users/alice/activities/create/{'posts' if kind == 'post' else 'comments'}/{i}",
                    object_id=f"https://bridge.test/actors/users/alice/objects/{'posts' if kind == 'post' else 'comments'}/{i}",
                    kind=kind,
                    body_markdown="original",
                    in_reply_to_object_id=(
                        None if i == 0 else "https://bridge.test/actors/users/alice/objects/posts/0"
                    ),
                    discord_channel_id=902,
                    discord_message_id=110 + i,
                )
            )
        s.add(
            old["MessageMapping"](
                id=22,
                source_platform="discord",
                source_id="source-22",
                activity_id="https://bridge.test/actors/users/alice/activities/create/posts/22",
                object_id="https://bridge.test/actors/users/alice/objects/posts/22",
                actor_url="https://bridge.test/actors/users/alice",
                community_actor_url="https://remote.test/c/news",
                discord_channel_id=902,
                discord_message_id=112,
            )
        )
        s.add(
            old["BridgeActorKey"](
                id=1,
                actor_url="https://bridge.test/actors/services/bridge",
                key_id="https://bridge.test/actors/services/bridge#main-key",
                key_format="pem",
                algorithm="rsa",
                public_key_data="service-public",
                private_key_data="service-private",
            )
        )
        s.add(
            old["RemoteSubscriber"](
                id=30,
                local_community_id=8,
                remote_actor_id="https://remote.test/u/bob",
                remote_inbox_url="https://remote.test/u/bob/inbox",
                follow_activity_id="https://remote.test/follow/30",
            )
        )
        s.add(
            old["CommunityThreadGroup"](
                id=40,
                community_actor_id="https://remote.test/c/news",
                source_channel_id=902,
                source_thread_id=1000,
                source_starter_message_id=1000,
            )
        )
        s.add(
            old["CommunityThreadGroupDelivery"](
                id=41,
                thread_group_id=40,
                discord_channel_id=902,
                discord_thread_id=1000,
                discord_starter_message_id=1000,
                role="source",
            )
        )
        for i in range(2):
            s.add(
                old["CommunityMessageGroup"](
                    id=42 + i,
                    community_actor_id="https://remote.test/c/news",
                    thread_group_id=40,
                    source_channel_id=902,
                    source_thread_id=1000,
                    source_message_id=120 + i,
                    parent_message_group_id=42 if i else None,
                )
            )
            s.add(
                old["CommunityMessageGroupDelivery"](
                    id=44 + i,
                    message_group_id=42 + i,
                    discord_channel_id=902,
                    discord_thread_id=1000,
                    discord_message_id=120 + i,
                    role="source",
                )
            )
        s.add(
            old["LocalCommunityThread"](
                id=50,
                local_community_id=8,
                ap_activity_id="https://bridge.test/actors/users/alice/activities/create/posts/50",
                ap_object_id="https://bridge.test/actors/users/alice/objects/posts/50",
                direction="outbound",
                origin_kind="local_host",
            )
        )
        s.add(
            old["LocalCommunityMessage"](
                id=51,
                local_community_thread_id=50,
                ap_activity_id="https://bridge.test/actors/users/alice/activities/create/comments/51",
                ap_object_id="https://bridge.test/actors/users/alice/objects/comments/51",
                parent_ap_object_id="https://bridge.test/actors/users/alice/objects/posts/50",
                direction="outbound",
            )
        )
        s.add(
            old["LocalCommunityThreadSurface"](
                id=52,
                local_community_thread_id=50,
                discord_forum_channel_id=900,
                discord_thread_id=2000,
                discord_starter_message_id=2000,
                role="host",
            )
        )
        s.add(
            old["LocalCommunityMessageSurface"](
                id=53,
                local_community_message_id=51,
                local_community_thread_surface_id=52,
                discord_forum_channel_id=900,
                discord_message_id=2001,
                parent_discord_message_id=2000,
                role="host",
            )
        )
        s.add(
            old["PostLink"](
                id=60,
                lemmy_post_id=60,
                discord_forum_channel_id=902,
                discord_forum_thread_id=3000,
                discord_starter_message_id=3000,
                direction="inbound",
            )
        )
        s.add(
            old["CommentLink"](
                id=61,
                lemmy_comment_id=61,
                lemmy_post_id=60,
                discord_forum_thread_id=3000,
                discord_message_id=3001,
                direction="inbound",
            )
        )
        s.add(
            old["ManagementAuditEvent"](
                id=70,
                action="community.created",
                result="success",
                target_type="community",
                target_id="8",
                actor_discord_user_id="1",
                after_json="{}",
            )
        )
        s.add(
            old["ActivityPubEventReceipt"](
                id=71,
                delivery_id="delivery-71",
                event_type="post.created",
                object_ap_id="https://bridge.test/objects/71",
                status="processed",
            )
        )
        s.commit()


def _evidence(db):
    """Capture every source column and exact DDL for rollback/preservation assertions."""
    with db.engine.connect() as c:
        names = (
            c.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*' ORDER BY name"
            )
            .scalars()
            .all()
        )
        return (
            c.exec_driver_sql("PRAGMA user_version").scalar_one(),
            c.exec_driver_sql(
                "SELECT type,name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name"
            ).all(),
            {
                n: [
                    dict(r)
                    for r in c.exec_driver_sql(
                        f'SELECT * FROM "{n}" ORDER BY id'
                    ).mappings()
                ]
                for n in names
            },
        )


def test_populated_release_upgrades_in_one_startup(legacy):
    """A released root, reply, keys and failed subscription survive without a replay."""
    _seed(legacy)
    before = _evidence(legacy)
    stamps = []

    def trace(c, cursor, statement, parameters, context, many):
        if statement.lower().startswith("pragma user_version") and "=" in statement:
            stamps.append(statement)

    event.listen(legacy.engine, "before_cursor_execute", trace)
    legacy.migrate()
    assert len(stamps) == 1 and stamps[0].replace(" ", "").endswith("=1")
    after = _evidence(legacy)
    assert after[0] == 1 and len(after[2]) == 45
    for name, rows in before[2].items():
        for row, new in zip(rows, after[2][name], strict=True):
            for col, value in row.items():
                if name == "local_communities" and col in {
                    "discord_guild_id",
                    "discord_forum_channel_id",
                }:
                    continue
                assert new[col] == value
    assert [
        r["conversation_root_object_id"] for r in after[2]["published_activity_objects"]
    ] == ["https://bridge.test/actors/users/alice/objects/posts/0"] * 2
    assert all(
        r["publication_context"] == "community" and r["content_revision"] == 1
        for r in after[2]["published_activity_objects"]
    )
    assert after[2]["message_mappings"][0]["publication_context"] == "community"
    assert after[2]["users"][0]["profile_version"] == 0
    legacy.migrate()
    assert _evidence(legacy) == after
    assert len(stamps) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_table",
        "missing_column",
        "missing_unique",
        "extra_fk",
        "view",
        "stage00",
        "schema_only",
        "future",
        "negative",
    ],
)
def test_unknown_source_or_header_is_rejected_unchanged(legacy, mutation):
    """Near-match source and unpublished development states cannot authorize writes."""
    with legacy.engine.begin() as c:
        if mutation == "extra_table":
            c.exec_driver_sql("CREATE TABLE rogue(id INTEGER)")
        elif mutation == "missing_column":
            c.exec_driver_sql("ALTER TABLE users DROP COLUMN created_at")
        elif mutation == "missing_unique":
            c.exec_driver_sql("DROP TABLE bridge_actor_follows")
            c.exec_driver_sql(
                "CREATE TABLE bridge_actor_follows(id INTEGER PRIMARY KEY, community_actor_id VARCHAR(512) NOT NULL, follow_activity_id VARCHAR(512),community_inbox_url VARCHAR(512),status VARCHAR(32) NOT NULL,created_at DATETIME NOT NULL,updated_at DATETIME NOT NULL)"
            )
        elif mutation == "extra_fk":
            ddl = c.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name='users'"
            ).scalar_one()
            ddl = (
                ddl.rstrip().removesuffix(")")
                + ", FOREIGN KEY (discord_user_id) REFERENCES bridge_actor_keys(key_id))"
            )
            c.exec_driver_sql("DROP TABLE users")
            c.exec_driver_sql(ddl)
        elif mutation == "view":
            c.exec_driver_sql("CREATE VIEW rogue AS SELECT 1")
        elif mutation in {"future", "negative"}:
            c.exec_driver_sql(
                "PRAGMA user_version=" + ("2" if mutation == "future" else "-1")
            )
    if mutation in {"stage00", "schema_only"}:
        legacy.close()
        with sqlite3.connect(legacy.engine.url.database) as c:
            for name in _evidence(legacy)[2]:
                c.execute(f'DROP TABLE "{name}"')
            if mutation == "stage00":
                c.executescript(
                    (
                        Path(__file__).parent / "fixtures/platform_v1_schema.sql"
                    ).read_text()
                )
        if mutation == "schema_only":
            legacy.create_all()
    before = _evidence(legacy)
    with pytest.raises(RuntimeError):
        legacy.migrate()
    assert _evidence(legacy) == before


@pytest.mark.parametrize("phase", ["created", "copied", "header"])
def test_failure_rolls_back_original_release_then_retry(legacy, monkeypatch, phase):
    """Faults at either internal phase or stamping undo every source DDL/data change."""
    _seed(legacy)
    before = _evidence(legacy)

    def reject(*args):
        raise RuntimeError("injected fault")

    if phase in {"created", "copied"}:
        name = (
            "_copy_source_rows"
            if phase == "created"
            else "_assert_preserved"
        )
        monkeypatch.setattr(direct, name, reject)
    else:

        def trace(c, cursor, statement, parameters, context, many):
            if statement.lower().startswith("pragma user_version") and "=" in statement:
                reject()

        event.listen(legacy.engine, "after_cursor_execute", trace)
    with pytest.raises(RuntimeError, match="injected fault"):
        legacy.migrate()
    assert _evidence(legacy) == before
    with legacy.engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    monkeypatch.undo()
    if phase == "header":
        event.remove(legacy.engine, "after_cursor_execute", trace)
    legacy.migrate()
    assert _evidence(legacy)[0] == 1


def test_concurrent_release_startup_rechecks_winner(legacy):
    """Three starters observe one committed current schema and one connector."""
    _seed(legacy)
    with ThreadPoolExecutor(3) as pool:
        list(pool.map(lambda _: legacy.migrate(), range(3)))
    data = _evidence(legacy)
    assert data[0] == 1 and len(data[2]["platform_connectors"]) == 1
    assert len(data[2]["published_activity_objects"]) == 2


def test_fresh_and_upgraded_semantic_manifests_match(legacy, tmp_path):
    """Default, FK, generated role, indexes and triggers agree on both entry paths."""
    _seed(legacy)
    legacy.migrate()
    fresh = Database(f"sqlite:///{tmp_path/'fresh.db'}")
    fresh.migrate()
    try:
        with legacy.engine.connect() as a, fresh.engine.connect() as b:
            for name in _evidence(fresh)[2]:
                assert migrations._table_manifest(
                    a, name
                ) == migrations._table_manifest(b, name), name
            # The retained SQL default must work without ORM-side defaults too.
            for conn in (a, b):
                conn.exec_driver_sql(
                    "INSERT INTO users (id,discord_user_id,activitypub_username,actor_url,inbox_url,outbox_url,followers_url,public_key_pem,private_key_pem,created_at,updated_at) VALUES (80,'80','default-user','https://bridge.test/actors/users/default-user','https://bridge.test/actors/users/default-user/inbox','https://bridge.test/actors/users/default-user/outbox','https://bridge.test/actors/users/default-user/followers','public','private',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                )
                assert (
                    conn.exec_driver_sql(
                        "SELECT profile_version FROM users WHERE id=80"
                    ).scalar_one()
                    == 0
                )
    finally:
        fresh.close()


@pytest.mark.parametrize(
    "fault", ["source_drop", "published_copy", "claim", "commit"]
)
def test_release_fault_boundaries_restore_exact_original(legacy, fault):
    """Real DROP, claim DML and commit failures restore every original row and DDL."""
    _seed(legacy)
    before = _evidence(legacy)
    drops = 0

    def interrupt(conn, cursor, statement, parameters, context, many):
        """Inject after the selected physical operation, never bypassing FK restoration."""
        nonlocal drops
        if fault == "source_drop" and statement.strip() == 'DROP TABLE main."published_activity_objects"':
            raise RuntimeError("fault boundary")
        if fault == "published_copy" and statement.lstrip().startswith('INSERT INTO main.published_activity_objects'):
            raise RuntimeError("fault boundary")
        if fault == "claim" and statement.startswith("UPDATE main.platform_channel_surfaces SET"):
            raise RuntimeError("fault boundary")

    def before_commit(conn):
        """Abort at the last runner boundary before the DBAPI commit."""
        if fault == "commit":
            raise RuntimeError("fault boundary")

    event.listen(legacy.engine, "after_cursor_execute", interrupt)
    event.listen(legacy.engine, "commit", before_commit)
    try:
        with pytest.raises(RuntimeError, match="fault boundary"):
            legacy.migrate()
        assert _evidence(legacy) == before
        with legacy.engine.connect() as c:
            assert c.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    finally:
        event.remove(legacy.engine, "after_cursor_execute", interrupt)
        event.remove(legacy.engine, "commit", before_commit)
    legacy.migrate()
    assert _evidence(legacy)[0] == 1


@pytest.mark.parametrize("rollback", [False, True])
def test_failed_fk_restore_invalidates_physical_connection(
    legacy, monkeypatch, rollback
):
    """An unrecoverable FK pragma never returns its unsafe connection to the pool."""
    invalidated = []

    def sabotage(conn, cursor, statement, parameters, context, many):
        """Start a DBAPI transaction immediately before restore to make its PRAGMA inert."""
        if statement == "PRAGMA foreign_keys=ON":
            conn.connection.driver_connection.execute("BEGIN")

    def observe(dbapi_connection, record, exception):
        """Record actual pool invalidation, rather than a call to a fake restore helper."""
        invalidated.append(True)

    event.listen(legacy.engine, "before_cursor_execute", sabotage)
    event.listen(legacy.engine, "invalidate", observe)
    if rollback:

        def fail(*args):
            raise RuntimeError("upgrade fault")

        monkeypatch.setattr(direct, "_assert_preserved", fail)
    try:
        with pytest.raises(RuntimeError, match="Failed to restore"):
            legacy.migrate()
        assert invalidated == [True]
    finally:
        event.remove(legacy.engine, "before_cursor_execute", sabotage)
        event.remove(legacy.engine, "invalidate", observe)
    with legacy.engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert c.exec_driver_sql("PRAGMA user_version").scalar_one() == (
            0 if rollback else 1
        )


@pytest.mark.parametrize(
    "corruption",
    ["owner", "guild", "parent", "copy_channel", "missing_parent_copy", "self_parent"],
)
def test_contradictory_source_fails_before_ddl(legacy, corruption):
    """Conflicting physical or parent evidence is diagnosed by row before normalization."""
    _seed(legacy)
    with legacy.engine.begin() as c:
        if corruption == "owner":
            c.exec_driver_sql(
                "UPDATE local_subscribers SET discord_channel_id=900 WHERE id=9"
            )
        elif corruption == "guild":
            c.exec_driver_sql(
                "UPDATE channel_community_subscriptions SET discord_guild_id=2 WHERE id=10"
            )
            c.exec_driver_sql(
                "INSERT INTO discord_channel_snapshots(id,discord_guild_id,discord_channel_id,channel_name,channel_type,created_at,updated_at) VALUES(80,1,902,'news','forum',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        elif corruption == "parent":
            c.exec_driver_sql(
                "UPDATE community_message_groups SET parent_message_group_id=999 WHERE id=43"
            )
        elif corruption == "copy_channel":
            c.exec_driver_sql(
                "UPDATE local_community_message_surfaces SET discord_forum_channel_id=901 WHERE id=53"
            )
        elif corruption == "missing_parent_copy":
            c.exec_driver_sql(
                "DELETE FROM community_message_group_deliveries WHERE id=44"
            )
        else:
            c.exec_driver_sql(
                "UPDATE community_message_groups SET parent_message_group_id=42 WHERE id=42"
            )
    before = _evidence(legacy)
    ddl = []

    def observe(conn, cursor, statement, parameters, context, many):
        """Capture every potential mutation after malformed source setup."""
        if statement.strip().split()[0].upper() in {
            "CREATE",
            "ALTER",
            "DROP",
            "INSERT",
            "UPDATE",
            "DELETE",
        }:
            ddl.append(statement)

    event.listen(legacy.engine, "before_cursor_execute", observe)
    with pytest.raises(RuntimeError, match="legacy|Missing target-local parent"):
        legacy.migrate()
    assert _evidence(legacy) == before and ddl == []


def test_backup_restore_does_not_repeat_upgrade(legacy, tmp_path):
    """An online snapshot of upgraded v1 reopens without touching identities or claims."""
    from src.db.backup import create_backup

    _seed(legacy)
    legacy.migrate()
    before = _evidence(legacy)
    path = create_backup(Path(legacy.engine.url.database), tmp_path / "backups")
    restored = Database(f"sqlite:///{path}")
    try:
        restored.migrate()
        assert _evidence(restored) == before
    finally:
        restored.close()


@pytest.mark.parametrize(
    "corruption", ["column", "check", "trigger", "unique", "connector"]
)
def test_current_header_does_not_authorize_schema_repairs(tmp_path, corruption):
    """A stamped v1 with incomplete behavior is rejected rather than implicitly repaired."""
    db = Database(f"sqlite:///{tmp_path/'current.db'}")
    db.migrate()
    with db.engine.begin() as c:
        if corruption == "column":
            c.exec_driver_sql("ALTER TABLE users DROP COLUMN display_name")
        elif corruption == "trigger":
            c.exec_driver_sql("DROP TRIGGER users_avatar_media_kind_insert")
        elif corruption == "connector":
            c.exec_driver_sql(
                "DELETE FROM platform_connectors WHERE connector_key='discord-primary'"
            )
        else:
            sql = c.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name='platform_channel_surfaces'"
            ).scalar_one()
            if corruption == "check":
                sql = sql.replace(" <= 1", " <= 2")
            else:
                sql = sql.replace(
                    "UNIQUE (local_community_id)", "UNIQUE (local_community_id, id)"
                )
            c.exec_driver_sql("DROP TABLE platform_channel_surfaces")
            c.exec_driver_sql(sql)
    before = _evidence(db)
    with pytest.raises(RuntimeError):
        db.migrate()
    assert _evidence(db) == before
    db.close()


@pytest.mark.asyncio
async def test_migrated_follow_accept_routes_existing_discord_claims(legacy):
    """A real Accept after upgrade promotes existing rows without republishing content."""
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.activitypub_handlers import dispatch_activitypub_event
    from src.activitypub_models import FollowLifecycleEvent
    from support.runtime import build_test_policy_service

    _seed(legacy)
    with legacy.engine.begin() as c:
        c.exec_driver_sql("UPDATE bridge_actor_follows SET status='pending'")
        c.exec_driver_sql(
            "UPDATE channel_community_subscriptions SET follow_activity_id='https://bridge.test/follows/3', initiated_by_discord_user_id='1' WHERE id=11"
        )
    legacy.migrate()
    before = _evidence(legacy)
    user = SimpleNamespace(send=AsyncMock())
    runtime = SimpleNamespace(
        database=legacy,
        bot=SimpleNamespace(fetch_user=AsyncMock(return_value=user)),
        bridge_policy_service=build_test_policy_service(legacy),
    )
    result = await dispatch_activitypub_event(
        FollowLifecycleEvent(
            event_type="follow.accepted",
            delivery_id="https://remote.test/accept/3",
            occurred_at=datetime.now(timezone.utc),
            community_actor_id="https://remote.test/c/news",
            actor_id="https://remote.test/c/news",
            object={"follow_activity_id": "https://bridge.test/follows/3"},
        ),
        runtime,
    )
    assert result.status == "processed"
    assert (
        legacy.remote_subscriptions.get_subscription_by_channel(903).status
        == "accepted"
    )
    assert (
        legacy.bridge_actor_follows.get_bridge_actor_follow(
            "https://remote.test/c/news"
        ).status
        == "accepted"
    )
    user.send.assert_awaited_once()
    after = _evidence(legacy)
    assert (
        after[2]["published_activity_objects"]
        == before[2]["published_activity_objects"]
    )
    assert (
        after[2]["platform_channel_surfaces"] == before[2]["platform_channel_surfaces"]
    )
    # Current local placement and neutral subscription writes retain their contracts.
    host = legacy.platform_surfaces.get_discord_host_placement(8)
    assert host.external_channel_id == "900"
    assert legacy.local_subscribers.list_discord_subscribers(community_id=8)


@pytest.mark.asyncio
async def test_migrated_remote_delivery_and_local_publish(legacy):
    """Existing remote/host/subscriber rows route new actions through real runtimes."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.activitypub_handlers import dispatch_activitypub_event
    from src.content_publish_service import ContentPublishService
    from src.local_communities.runtime import LocalCommunityRuntime
    from src.fedify_gateway_client import PublishLocalCommunityContentResult
    from support.activitypub import build_post_created_event
    from support.gateway import build_gateway_mock
    from support.discord import (
        build_bot,
        build_forum_channel_tuple_result,
        build_forum_channel_object_result,
        build_thread,
        build_starter_message,
    )
    from support.runtime import build_community_runtime, build_test_policy_service

    _seed(legacy)
    legacy.migrate()
    forum = build_forum_channel_tuple_result(
        channel_id=902, thread_id=4000, starter_message_id=4001
    )
    bot = build_bot(forum_channels={902: forum})
    remote = build_community_runtime(
        legacy, fedify_gateway=build_gateway_mock(), bot=bot
    )
    policy = build_test_policy_service(legacy)
    result = await dispatch_activitypub_event(
        build_post_created_event(
            object_id="https://remote.test/post/new",
            community_actor_id="https://remote.test/c/news",
        ),
        SimpleNamespace(
            database=legacy,
            bot=bot,
            community_runtime=remote,
            bridge_policy_service=policy,
        ),
    )
    assert result.status == "processed"
    copies = legacy.discord_fanout_groups.get_thread_deliveries(
        legacy.discord_fanout_groups.get_thread_group_by_ap_object(
            "https://remote.test/post/new"
        ).id
    )
    assert {r.discord_channel_id for r in copies} == {902}
    gateway = build_gateway_mock()
    gateway.publish_local_community_content = AsyncMock(
        return_value=PublishLocalCommunityContentResult(
            "https://bridge.test/actors/communities/news/activities/new",
            "https://bridge.test/actors/communities/news/objects/new",
            "https://bridge.test/actors/communities/news",
            0,
            0,
        )
    )
    bot = build_bot(
        forum_channels={
            901: build_forum_channel_object_result(
                channel_id=901, thread_id=5000, starter_message_id=5001
            )
        }
    )
    runtime = LocalCommunityRuntime(
        database=legacy,
        fedify_gateway=gateway,
        bridge_prefix="[bridge]",
        bot=bot,
        bridge_policy_service=policy,
        content_publish_service=ContentPublishService(
            database=legacy,
            fedify_gateway=gateway,
            bridge_prefix="[bridge]",
            bridge_policy_service=policy,
        ),
    )
    published = await runtime.handle_discord_thread_create(
        thread=build_thread(thread_id=6000, channel_id=900),
        starter_message=build_starter_message(message_id=6000, author_id=1),
    )
    assert published.status == "published"
    thread = legacy.local_community_content.get_local_community_thread_by_ap_object_id(
        "https://bridge.test/actors/communities/news/objects/new"
    )
    assert {
        s.discord_forum_channel_id
        for s in legacy.local_community_surfaces.list_local_community_thread_surfaces(
            thread.id
        )
    } == {900, 901}


def test_migrated_actor_read_contract_retains_keys_and_urls(legacy):
    """Authenticated gateway reads expose the same historical actor identities."""
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from src.http_api import create_http_app
    from support.runtime import build_test_policy_service

    _seed(legacy)
    legacy.migrate()
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
        for route, key, value in [
            (
                "/internal/fedify/actors/users/alice",
                "actor_url",
                "https://bridge.test/actors/users/alice",
            ),
            (
                "/internal/fedify/actors/communities/news",
                "actor_url",
                "https://bridge.test/actors/communities/news",
            ),
        ]:
            response = client.get(route, headers={"Authorization": "Bearer secret"})
            assert response.status_code == 200 and response.json()[key] == value
            assert response.json()["private_key_pem"] == (
                "private" if "/users/" in route else "cprivate"
            )


@pytest.mark.parametrize(
    "table",
    [
        "message_mappings",
        "published_activity_objects",
        "local_community_message_surfaces",
    ],
)
def test_migrated_composite_copies_reject_crossed_channel(legacy, table):
    """Real migrated rows reject cross-connector and parent-channel UPDATE with FK ON."""
    from sqlalchemy.exc import IntegrityError

    _seed(legacy)
    legacy.migrate()
    with legacy.immediate_session() as s:
        other = legacy.platform_surfaces.add_connector(
            s,
            connector_key="fluxer-test",
            platform="fluxer",
            instance_origin="https://fluxer.test",
        )
        foreign = legacy.platform_surfaces.ensure_channel(
            s, connector_key="fluxer-test", external_channel_id="900", kind="forum"
        )
        connector, channel = other.id, foreign.id
    with pytest.raises(IntegrityError):
        with legacy.engine.begin() as c:
            assert c.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            if table == "local_community_message_surfaces":
                c.exec_driver_sql(
                    "UPDATE local_community_message_surfaces SET connector_id=?,channel_surface_id=? WHERE id=53",
                    (connector, channel),
                )
            else:
                c.exec_driver_sql(
                    f"UPDATE {table} SET source_connector_id=?", (connector,)
                )
    with legacy.engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_migrated_owner_exclusivity_remains_database_enforced(legacy):
    """Direct SQL cannot assign a second typed owner to the migrated host channel."""
    from sqlalchemy.exc import IntegrityError

    _seed(legacy)
    legacy.migrate()
    with pytest.raises(IntegrityError):
        with legacy.engine.begin() as c:
            c.exec_driver_sql(
                "UPDATE platform_channel_surfaces SET local_subscriber_id=9 WHERE local_community_id=8"
            )
    with legacy.engine.connect() as c:
        assert (
            c.exec_driver_sql(
                "SELECT role FROM platform_channel_surfaces WHERE local_community_id=8"
            ).scalar_one()
            == "local_host"
        )
        assert c.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_release_recognition_accepts_incidental_sql_formatting(tmp_path):
    """Whitespace, identifier quotes and constraint names do not alter source identity."""
    ddl = (Path(__file__).parent / "fixtures/release_v0_1_0_schema.sql").read_text()
    ddl = (
        ddl.replace("CREATE TABLE users (", 'CREATE TABLE "users" (')
        .replace("CHECK (", "CHECK\n( ")
        .replace("ck_bridge_actor_keys_singleton", "renamed_singleton")
    )
    path = tmp_path / "formatted.db"
    with sqlite3.connect(path) as c:
        c.executescript(ddl)
    db = Database(f"sqlite:///{path}")
    try:
        _seed(db)
        db.migrate()
        assert _evidence(db)[0] == 1
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["insert", "update"])
def test_real_release_pao_composite_fk_rejects_valid_crossed_sql(legacy, operation):
    """Fully populated INSERT/UPDATE fails specifically on the migrated composite FK."""
    from sqlalchemy.exc import IntegrityError
    from src.models import PublishedActivityObject

    _seed(legacy)
    legacy.migrate()
    with legacy.immediate_session() as s:
        other = legacy.platform_surfaces.add_connector(
            s,
            connector_key="fluxer-other",
            platform="fluxer",
            instance_origin="https://other.test",
        )
        host = legacy.platform_surfaces.ensure_channel(
            s, connector_key="discord-primary", external_channel_id="902"
        )
        other_id, host_id = other.id, host.id
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        with legacy.immediate_session() as s:
            if operation == "update":
                row = s.get(PublishedActivityObject, 20)
                row.source_connector_id = other_id
            else:
                s.add(
                    PublishedActivityObject(
                        actor_username="alice",
                        actor_url="https://bridge.test/actors/users/alice",
                        publication_context="community",
                        community_actor_url="https://remote.test/c/news",
                        activity_id="https://bridge.test/actors/users/alice/activities/create/posts/crossed",
                        object_id="https://bridge.test/actors/users/alice/objects/posts/crossed",
                        kind="post",
                        body_markdown="valid",
                        source_connector_id=other_id,
                        source_channel_surface_id=host_id,
                    )
                )


def test_release_snapshot_loaded_once_per_inspection_and_kept_immutable(legacy, monkeypatch):
    """Startup reads a separate source under each lock and never changes captured evidence."""
    from copy import deepcopy

    _seed(legacy)
    real_capture = direct.capture_source_evidence
    captures = []
    transaction = None

    def trace(connection, cursor, statement, parameters, context, many):
        """Record the actual SQLite lock active when historical rows are read."""
        nonlocal transaction
        if statement in {'BEGIN', 'BEGIN IMMEDIATE'}:
            transaction = statement

    def capture(connection):
        """Retain the evidence and its value before downstream migration stages use it."""
        snapshot = real_capture(connection)
        captures.append((connection, transaction, snapshot, deepcopy(snapshot)))
        return snapshot

    event.listen(legacy.engine, 'before_cursor_execute', trace)
    monkeypatch.setattr(direct, 'capture_source_evidence', capture)
    legacy.migrate()
    assert len(captures) == 2
    assert [entry[1] for entry in captures] == ['BEGIN', 'BEGIN IMMEDIATE']
    assert captures[0][0] is not captures[1][0]
    assert captures[0][2] is not captures[1][2]
    assert all(snapshot == original for _, _, snapshot, original in captures)
    legacy.migrate()
    assert len(captures) == 2  # Current-v1 admission does not reload legacy rows.
