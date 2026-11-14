"""Verified NodeBB distribution drives real community SQLite and Discord reservations."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.activitypub_models import RemoteGroupAnnounceEvent
from src.community_sync.nodebb_inbound import handle_nodebb_announcement
from src.community_sync.runtime import CommunityRuntime
from src.content_publish_service import ContentPublishService
from src.db import Database
from support.runtime import build_test_policy_service

GROUP = "https://nodebb.example/category/42"
ROOT = "https://nodebb.example/post/101"
REPLY = "https://nodebb.example/post/102"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/nodebb"


def _fixture(name: str) -> dict:
    """Load the checked pinned NodeBB wire fixture."""
    return json.loads((FIXTURES / name).read_text())


def _event(filename: str) -> RemoteGroupAnnounceEvent:
    """Represent a Fedify-authenticated Group distribution at Python boundary."""
    raw = _fixture(filename)
    target = raw["object"]
    inline = target if isinstance(target, dict) else None
    return RemoteGroupAnnounceEvent.model_validate({
        "event_type": "remote.group_announced", "actor_id": raw["actor"],
        "community_actor_id": raw["actor"], "delivery_id": raw["id"],
        "occurred_at": datetime.now(timezone.utc), "source_activity_json": raw,
        "object": {"ap_id": target if isinstance(target, str) else target["id"],
                   "inline_object": inline, "inner_create_id": None},
    })


def _runtime(tmp_path: Path):
    """Create actual v1 database/follower and a fake outer Discord transport."""
    db = Database(f"sqlite:///{tmp_path / 'nodebb.db'}")
    db.create_all()
    db.remote_subscriptions.create_subscription(
        discord_channel_id=100, lemmy_community_actor_id=GROUP,
        lemmy_community_name="Discussion", lemmy_community_id=None,
        community_inbox_url=GROUP + "/inbox", status="accepted",
    )
    db.bridge_actor_follows.create_bridge_actor_follow(
        community_actor_id=GROUP, follow_activity_id=GROUP + "/follows/one",
        community_inbox_url=GROUP + "/inbox", status="accepted",
    )
    with db.session() as session:
        from src.models import BridgeActorFollow
        follow = session.query(BridgeActorFollow).first()
        follow.remote_software_profile = "nodebb"
    forum = SimpleNamespace(create_thread=AsyncMock(return_value=(SimpleNamespace(id=200), SimpleNamespace(id=300))))
    thread = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=400)))

    async def fetch_forum_channel(_channel):
        return forum

    async def get_thread_by_id(_id):
        return thread

    async def ready():
        return None

    bot = SimpleNamespace(fetch_forum_channel=fetch_forum_channel,
                          get_thread_by_id=get_thread_by_id, wait_until_bridge_ready=ready)
    policy = build_test_policy_service(db)
    publish = ContentPublishService(database=db, fedify_gateway=AsyncMock(), bridge_prefix="[bridge]", bridge_policy_service=policy)
    rt = CommunityRuntime(database=db, content_publish_service=publish, bot=bot)
    return db, SimpleNamespace(database=db, community_runtime=rt, bot=bot,
                               bridge_policy_service=policy), forum, thread


@pytest.mark.asyncio
async def test_group_announce_uri_root_reply_author_preserved(tmp_path: Path, monkeypatch) -> None:
    """Signed Group URI root/reply places one thread/message with Person owner."""
    db, runtime, forum, thread = _runtime(tmp_path)
    docs = {GROUP: _fixture("category_actor.json"), ROOT: _fixture("root_note.json"),
            REPLY: _fixture("reply_note.json")}
    docs[ROOT]["attributedTo"] = "https://author.example/actors/alice"
    docs[REPLY]["attributedTo"] = "https://author.example/actors/alice"

    class FixtureFetch:
        """Stand-in for the public-IP-bounded HTTPS adapter at the network edge."""
        def __init__(self, _origin, _policy):
            pass

        async def __call__(self, url, _accept):
            return docs[url]

    import src.community_sync.nodebb_inbound as module
    monkeypatch.setattr(module, "SecureNodeBBJSON", FixtureFetch)
    try:
        root = await handle_nodebb_announcement(_event("announce_root_uri.json"), runtime)
        reply = await handle_nodebb_announcement(_event("announce_reply_uri.json"), runtime)
        assert (root.status, reply.status) == ("processed", "processed")
        assert forum.create_thread.await_count == 1
        assert thread.send.await_count == 1
        assert forum.create_thread.await_args.kwargs["suppress_embeds"] is True
        assert thread.send.await_args.kwargs["suppress_embeds"] is True
        row = db.discord_fanout_groups.get_thread_group_by_ap_object(ROOT, community_actor_id=GROUP)
        assert row.source_owner_actor_id == "https://author.example/actors/alice"
        row = db.discord_fanout_groups.get_message_group_by_ap_object(REPLY, community_actor_id=GROUP)
        assert row.source_owner_actor_id == "https://author.example/actors/alice"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unaccepted_group_is_denied_before_network_or_discord(tmp_path: Path, monkeypatch) -> None:
    """An expired Group Follow never permits signed Announce to create a host."""
    db, runtime, forum, _ = _runtime(tmp_path)
    import src.community_sync.nodebb_inbound as module
    calls = []
    monkeypatch.setattr(module, "SecureNodeBBJSON", lambda *_: calls.append("network"))
    try:
        with db.session() as session:
            from src.models import BridgeActorFollow
            follow = session.query(BridgeActorFollow).first()
            follow.status = "pending"
        result = await handle_nodebb_announcement(_event("announce_root_uri.json"), runtime)
        assert result.status == "skipped"
        assert not calls and forum.create_thread.await_count == 0
        assert db.discord_fanout_groups.get_thread_group_by_ap_object(ROOT, community_actor_id=GROUP) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_historical_reply_requires_group_outbox_membership(tmp_path: Path, monkeypatch) -> None:
    """A reply after Follow backfills an old root only with exact outbox evidence."""
    db, runtime, forum, thread = _runtime(tmp_path)
    docs = {GROUP: _fixture("category_actor.json"),
            ROOT: _fixture("root_note.json"), REPLY: _fixture("reply_note.json"),
            GROUP + "/outbox": _fixture("category_outbox_with_historical_root.json")}
    class FixtureFetch:
        def __init__(self, _origin, _policy):
            pass
        async def __call__(self, url, _accept):
            return docs[url]
    import src.community_sync.nodebb_inbound as module
    import src.community_sync.nodebb_backfill as backfill
    monkeypatch.setattr(module, "SecureNodeBBJSON", FixtureFetch)
    monkeypatch.setattr(backfill, "SecureNodeBBJSON", FixtureFetch)
    try:
        result = await handle_nodebb_announcement(_event("announce_reply_uri.json"), runtime)
        assert result.status == "processed"
        assert forum.create_thread.await_count == 1 and thread.send.await_count == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_nested_create_uses_outer_announce_and_immutable_owner(tmp_path: Path, monkeypatch) -> None:
    """Announce(Create(Note)) creates one root, without inventing a Person signature."""
    db, runtime, forum, _ = _runtime(tmp_path)
    raw = _fixture("announce_nested_create.json")
    inner = raw["object"]
    event = RemoteGroupAnnounceEvent.model_validate({
        "event_type": "remote.group_announced", "actor_id": raw["actor"],
        "community_actor_id": raw["actor"], "delivery_id": raw["id"],
        "occurred_at": datetime.now(timezone.utc), "source_activity_json": raw,
        "object": {"ap_id": inner["object"]["id"], "inline_object": inner["object"],
                   "inner_create_id": inner["id"]},
    })
    import src.community_sync.nodebb_inbound as module
    class FixtureFetch:
        def __init__(self, _origin, _policy):
            pass
        async def __call__(self, url, _accept):
            # Inline Note avoids a Note fetch, but attribution may still fetch
            # the author's Person actor for preferredUsername.
            if url == "https://nodebb.example/uid/7":
                return {"id": url, "type": "Person", "preferredUsername": "alice"}
            assert url == GROUP
            return _fixture("category_actor.json")
    monkeypatch.setattr(module, "SecureNodeBBJSON", FixtureFetch)
    try:
        result = await handle_nodebb_announcement(event, runtime)
        assert result.status == "processed"
        assert forum.create_thread.await_count == 1
        row = db.discord_fanout_groups.get_thread_group_by_ap_object(ROOT, community_actor_id=GROUP)
        assert row.source_owner_actor_id == "https://nodebb.example/uid/7"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_mismatched_note_audience_rejected_before_host(tmp_path: Path, monkeypatch) -> None:
    """A Group-signed distribution cannot place a Note addressed to another Group."""
    db, runtime, forum, _ = _runtime(tmp_path)
    docs = {GROUP: _fixture("category_actor.json"), ROOT: _fixture("root_note.json")}
    docs[ROOT]["audience"] = "https://nodebb.example/category/other"
    import src.community_sync.nodebb_inbound as module
    class FixtureFetch:
        def __init__(self, _origin, _policy):
            pass
        async def __call__(self, url, _accept):
            return docs[url]
    monkeypatch.setattr(module, "SecureNodeBBJSON", FixtureFetch)
    try:
        result = await handle_nodebb_announcement(_event("announce_root_uri.json"), runtime)
        assert result.status == "skipped" and forum.create_thread.await_count == 0
        assert db.discord_fanout_groups.get_thread_group_by_ap_object(ROOT, community_actor_id=GROUP) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_nodebb_author_preferred_username_used_for_root_and_reply(tmp_path: Path, monkeypatch) -> None:
    """A numeric NodeBB actor UID renders its AP preferredUsername on both surfaces."""
    db, runtime, forum, thread = _runtime(tmp_path)
    owner = "https://nodebb.example/uid/2"
    docs = {
        GROUP: _fixture("category_actor.json"),
        ROOT: _fixture("root_note.json"),
        REPLY: _fixture("reply_note.json"),
        owner: {"id": owner, "type": "Person", "preferredUsername": "nachitima"},
    }
    docs[ROOT]["attributedTo"] = owner
    docs[REPLY]["attributedTo"] = owner

    class FixtureFetch:
        """Supply signed-object fixtures at the network boundary."""
        def __init__(self, _origin, _policy):
            pass

        async def __call__(self, url, _accept):
            return docs[url]

    import src.community_sync.nodebb_inbound as module
    monkeypatch.setattr(module, "SecureNodeBBJSON", FixtureFetch)
    try:
        assert (await handle_nodebb_announcement(_event("announce_root_uri.json"), runtime)).status == "processed"
        assert (await handle_nodebb_announcement(_event("announce_reply_uri.json"), runtime)).status == "processed"
        starter_body = forum.create_thread.await_args.kwargs["content"]
        reply_body = thread.send.await_args.args[0]
        assert "nachitima@nodebb.example" in starter_body
        assert "nachitima@nodebb.example" in reply_body
        assert "2@nodebb.example" not in starter_body
        assert "2@nodebb.example" not in reply_body
        root_row = db.discord_fanout_groups.get_thread_group_by_ap_object(ROOT, community_actor_id=GROUP)
        reply_row = db.discord_fanout_groups.get_message_group_by_ap_object(REPLY, community_actor_id=GROUP)
        assert root_row.source_owner_actor_id == owner
        assert reply_row.source_owner_actor_id == owner
    finally:
        db.close()


@pytest.mark.asyncio
async def test_nodebb_author_unavailable_falls_back_without_changing_owner(tmp_path: Path, monkeypatch) -> None:
    """Failure to fetch an author profile never blocks verified Group delivery."""
    db, runtime, forum, _ = _runtime(tmp_path)
    owner = "https://nodebb.example/uid/2"
    docs = {GROUP: _fixture("category_actor.json"), ROOT: _fixture("root_note.json")}
    docs[ROOT]["attributedTo"] = owner
    calls: list[str] = []

    class FixtureFetch:
        """Emulate a missing optional author actor while root Note is available."""
        def __init__(self, _origin, _policy):
            pass

        async def __call__(self, url, _accept):
            calls.append(url)
            if url == owner:
                raise OSError("actor temporarily unavailable")
            return docs[url]

    import src.community_sync.nodebb_inbound as module
    monkeypatch.setattr(module, "SecureNodeBBJSON", FixtureFetch)
    try:
        result = await handle_nodebb_announcement(_event("announce_root_uri.json"), runtime)
        assert result.status == "processed"
        assert calls.count(owner) == 1
        assert "2@nodebb.example" in forum.create_thread.await_args.kwargs["content"]
        row = db.discord_fanout_groups.get_thread_group_by_ap_object(ROOT, community_actor_id=GROUP)
        assert row.source_owner_actor_id == owner
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [
    {"id": "https://other.example/uid/2", "type": "Person", "preferredUsername": "forged"},
    {"id": "https://nodebb.example/uid/2", "type": "Person", "preferredUsername": "forged@other.example"},
])
async def test_nodebb_author_ignores_untrusted_profile_labels(
    tmp_path: Path, monkeypatch, profile: dict,
) -> None:
    """Unrelated actors and malformed handles never replace the saved owner."""
    db, runtime, forum, _ = _runtime(tmp_path)
    owner = "https://nodebb.example/uid/2"
    docs = {GROUP: _fixture("category_actor.json"), ROOT: _fixture("root_note.json"), owner: profile}
    docs[ROOT]["attributedTo"] = owner

    class FixtureFetch:
        """Return a claimed profile with invalid identity or attribution label."""
        def __init__(self, _origin, _policy):
            pass

        async def __call__(self, url, _accept):
            return docs[url]

    import src.community_sync.nodebb_inbound as module
    monkeypatch.setattr(module, "SecureNodeBBJSON", FixtureFetch)
    try:
        result = await handle_nodebb_announcement(_event("announce_root_uri.json"), runtime)
        assert result.status == "processed"
        content = forum.create_thread.await_args.kwargs["content"]
        assert "forged" not in content
        assert "2@nodebb.example" in content
        row = db.discord_fanout_groups.get_thread_group_by_ap_object(ROOT, community_actor_id=GROUP)
        assert row.source_owner_actor_id == owner
    finally:
        db.close()
