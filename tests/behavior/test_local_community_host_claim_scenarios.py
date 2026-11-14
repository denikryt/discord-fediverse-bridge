"""CP21: signed Group creates reserve their tenant's host before Discord REST."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.activitypub_models import ActivityPubEvent
from src.bridge_policy import BridgePolicyService
from src.db import Database
from src.local_communities.inbound_create import LocalCommunityInboundCreateHandler

GROUP_A = "https://bridge.example/actors/communities/a"
GROUP_B = "https://bridge.example/actors/communities/b"
AUTHOR = "https://remote.example/actors/users/alice"
ROOT = AUTHOR + "/objects/posts/1"
SERVICE = "https://remote.example/actors/services/main"


def _setup(tmp_path):
    """Persist two independent accepted Groups for one remote subscriber."""
    db = Database(f"sqlite:///{tmp_path / 'cp21.sqlite'}")
    db.create_all()
    communities = []
    for n, group in enumerate((GROUP_A, GROUP_B), start=1):
        community = db.local_communities.create_local_community(
            discord_guild_id=n, discord_forum_channel_id=500 + n,
            slug=f"community-{n}", display_name=f"Community {n}", summary=None,
            created_by_discord_user_id="3", actor_url=group,
            inbox_url=group + "/inbox", outbox_url=group + "/outbox",
            followers_url=group + "/followers", public_key_pem="pub", private_key_pem="priv",
        )
        # Same-actor accepted relationship is an existing, independently signed
        # federation path. Host tenant isolation must hold for this path too.
        db.remote_subscribers.create_remote_subscriber(
            local_community_id=community.id, remote_actor_id=AUTHOR,
            remote_inbox_url=AUTHOR + "/inbox", follow_activity_id=f"https://remote.example/follow/{n}",
        )
        communities.append(community)
    policy = BridgePolicyService(settings=SimpleNamespace(
        federation_allowlist=[], federation_blocklist=[],
        discord_guild_allowlist=[], discord_guild_blocklist=[], bridge_super_admin_user_ids=[],
    ), repository=db.bridge_policy_entries)
    return db, communities, policy


def _event(group=GROUP_A, *, activity="create/1", owner=AUTHOR, comment=False, parent=ROOT):
    """Construct one verified Create addressed to a particular local Group."""
    obj_id = AUTHOR + "/objects/comments/1" if comment else ROOT
    obj = dict(ap_id=obj_id, kind="comment" if comment else "post",
        lemmy_id=0, published_at="2026-11-12T01:00:00Z", author_name="alice",
        body_markdown="hello", url=obj_id, attributed_to_actor_id=owner)
    if comment:
        obj.update(post_ap_id=ROOT, post_lemmy_id=0, parent_ap_id=parent)
    return ActivityPubEvent.model_validate({
        "event_type": "comment.created" if comment else "post.created",
        "delivery_id": "https://remote.example/activities/" + activity,
        "occurred_at": "2026-11-12T01:00:00Z", "community_actor_id": group,
        "verified_destination_group_actor_id": group,
        "actor_id": AUTHOR, "verified_sender_actor_id": AUTHOR,
        "source_activity_json": {"type": "Create", "actor": AUTHOR,
            "object": {"type": "Note", "id": obj_id, "attributedTo": owner}},
        "object": obj,
    })


def _handler(db, policy, *, bot):
    """Use real handler and SQLite with only Discord/sibling/network outer fakes."""
    return LocalCommunityInboundCreateHandler(
        database=db, surface_fanout=AsyncMock(), federation_fanout=AsyncMock(),
        discord_client_provider=lambda: bot, bridge_policy_service=policy,
    )


@pytest.mark.asyncio
async def test_same_remote_object_has_independent_host_in_two_authorized_groups(tmp_path):
    """Group A Create followed by B Create yields two different host rows."""
    db, groups, policy = _setup(tmp_path)
    async def created(*_, **__):
        idx = created.calls = getattr(created, 'calls', 0) + 1
        return (SimpleNamespace(id=700 + idx), SimpleNamespace(id=800 + idx))
    forum = SimpleNamespace(create_thread=created)
    bot = SimpleNamespace(wait_until_bridge_ready=AsyncMock(), fetch_forum_channel=AsyncMock(return_value=forum))
    h = _handler(db, policy, bot=bot)
    assert (await h.handle_post(_event(GROUP_A), runtime=None)).status == "processed"
    assert (await h.handle_post(_event(GROUP_B), runtime=None)).status == "processed"
    assert created.calls == 2
    a = db.local_community_content.get_local_community_thread_by_ap_object_id(ROOT, local_community_id=groups[0].id)
    b = db.local_community_content.get_local_community_thread_by_ap_object_id(ROOT, local_community_id=groups[1].id)
    assert a.id != b.id
    assert a.source_owner_actor_id == AUTHOR and b.source_owner_actor_id == AUTHOR
    assert a.host_delivery_state == b.host_delivery_state == "delivered"
    db.close()


@pytest.mark.asyncio
async def test_concurrent_different_delivery_ids_claim_one_host_before_rest(tmp_path):
    """Second signed Create cannot send while first Discord REST is outstanding."""
    db, _, policy = _setup(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    counter = 0
    async def send(**_):
        nonlocal counter
        counter += 1
        started.set()
        await release.wait()
        return (SimpleNamespace(id=701), SimpleNamespace(id=801))
    forum = SimpleNamespace(create_thread=send)
    bot = SimpleNamespace(wait_until_bridge_ready=AsyncMock(), fetch_forum_channel=AsyncMock(return_value=forum))
    h = _handler(db, policy, bot=bot)
    first = asyncio.create_task(h.handle_post(_event(activity="create/first"), runtime=None))
    await asyncio.wait_for(started.wait(), timeout=4)
    second = await asyncio.wait_for(h.handle_post(_event(activity="announce/second"), runtime=None), timeout=4)
    assert second.status == "skipped"
    assert counter == 1
    release.set()
    assert (await first).status == "processed"
    assert counter == 1
    db.close()


@pytest.mark.asyncio
async def test_conflicting_owner_duplicate_does_not_fanout_or_change_original(tmp_path):
    """A forged later envelope cannot replace the immutable first Create owner."""
    db, groups, policy = _setup(tmp_path)
    forum = SimpleNamespace(create_thread=AsyncMock(return_value=(SimpleNamespace(id=700), SimpleNamespace(id=800))))
    bot = SimpleNamespace(wait_until_bridge_ready=AsyncMock(), fetch_forum_channel=AsyncMock(return_value=forum))
    h = _handler(db, policy, bot=bot)
    assert (await h.handle_post(_event(), runtime=None)).status == "processed"
    h.surface_fanout.reset_mock()
    h.federation_fanout.reset_mock()
    result = await h.handle_post(_event(activity="create/spoof", owner="https://evil.example/person"), runtime=None)
    assert result.status == "skipped"
    forum.create_thread.assert_awaited_once()
    assert forum.create_thread.await_args.kwargs["suppress_embeds"] is True
    h.surface_fanout.fanout_thread_to_local_subscribers.assert_not_awaited()
    h.federation_fanout.relay_create.assert_not_awaited()
    original = db.local_community_content.get_local_community_thread_by_ap_object_id(ROOT, local_community_id=groups[0].id)
    assert original.source_owner_actor_id == AUTHOR
    db.close()


@pytest.mark.asyncio
async def test_timeout_after_possible_send_marks_unknown_and_never_resends(tmp_path):
    """Ambiguous Discord outcome remains nonretryable for that tenant/object."""
    db, groups, policy = _setup(tmp_path)
    forum = SimpleNamespace(create_thread=AsyncMock(side_effect=TimeoutError("response lost")))
    bot = SimpleNamespace(wait_until_bridge_ready=AsyncMock(), fetch_forum_channel=AsyncMock(return_value=forum))
    h = _handler(db, policy, bot=bot)
    await h.handle_post(_event(), runtime=None)
    original = db.local_community_content.get_local_community_thread_by_ap_object_id(ROOT, local_community_id=groups[0].id)
    assert original.host_delivery_state == "unknown"
    result = await h.handle_post(_event(activity="announce/repeat"), runtime=None)
    assert result.status == "skipped"
    forum.create_thread.assert_awaited_once()
    db.close()


@pytest.mark.asyncio
async def test_reply_parent_and_host_attempt_belong_to_same_tenant(tmp_path):
    """The same reply object can be placed once per Group, never using its neighbor's root."""
    db, groups, policy = _setup(tmp_path)
    forum = SimpleNamespace(create_thread=AsyncMock(return_value=(SimpleNamespace(id=701), SimpleNamespace(id=801))))
    thread = SimpleNamespace(id=701, guild_id=1, send=AsyncMock(return_value=SimpleNamespace(id=802)))
    bot = SimpleNamespace(wait_until_bridge_ready=AsyncMock(), fetch_forum_channel=AsyncMock(return_value=forum), get_thread_by_id=AsyncMock(return_value=thread))
    h = _handler(db, policy, bot=bot)
    assert (await h.handle_post(_event(), runtime=None)).status == "processed"
    assert (await h.handle_comment(_event(comment=True), runtime=None)).status == "processed"
    assert (await h.handle_comment(_event(GROUP_B, comment=True), runtime=None)).status == "skipped"
    assert thread.send.await_count == 1
    assert thread.send.await_args.kwargs["suppress_embeds"] is True
    original = db.local_community_content.get_local_community_message_by_ap_object_id(AUTHOR + "/objects/comments/1", local_community_id=groups[0].id)
    assert original.source_owner_actor_id == AUTHOR and original.host_delivery_state == "delivered"
    db.close()

@pytest.mark.asyncio
async def test_two_groups_can_host_same_reply_without_reusing_foreign_parent(tmp_path):
    """Both subscribed Groups accept the same Note while keeping local surfaces separate."""
    db, groups, policy = _setup(tmp_path)
    async def make_thread(**kwargs):
        group_number = 1 if kwargs.get('name') == 'Forum A' else make_thread.calls + 1
        make_thread.calls = group_number
        return (SimpleNamespace(id=700 + group_number), SimpleNamespace(id=800 + group_number))
    make_thread.calls = 0
    forum = SimpleNamespace(create_thread=make_thread)
    threads = {
        701: SimpleNamespace(id=701, guild_id=1, send=AsyncMock(return_value=SimpleNamespace(id=901))),
        702: SimpleNamespace(id=702, guild_id=2, send=AsyncMock(return_value=SimpleNamespace(id=902))),
    }
    async def get_thread(identifier):
        return threads[identifier]
    bot = SimpleNamespace(wait_until_bridge_ready=AsyncMock(), fetch_forum_channel=AsyncMock(return_value=forum), get_thread_by_id=get_thread)
    h = _handler(db, policy, bot=bot)
    assert (await h.handle_post(_event(GROUP_A), runtime=None)).status == 'processed'
    assert (await h.handle_post(_event(GROUP_B), runtime=None)).status == 'processed'
    assert (await h.handle_comment(_event(GROUP_A, comment=True), runtime=None)).status == 'processed'
    assert (await h.handle_comment(_event(GROUP_B, comment=True), runtime=None)).status == 'processed'
    a = db.local_community_content.get_local_community_message_by_ap_object_id(AUTHOR + '/objects/comments/1', local_community_id=groups[0].id)
    b = db.local_community_content.get_local_community_message_by_ap_object_id(AUTHOR + '/objects/comments/1', local_community_id=groups[1].id)
    assert a.id != b.id and a.local_community_thread_id != b.local_community_thread_id
    threads[701].send.assert_awaited_once()
    threads[702].send.assert_awaited_once()
    db.close()


@pytest.mark.asyncio
async def test_foreign_nested_reply_parent_refused_before_host_send(tmp_path):
    """A parent Note placed only in A cannot authorize a nested reply in B."""
    db, groups, policy = _setup(tmp_path)
    async def created(**_):
        created.calls += 1
        return (SimpleNamespace(id=700 + created.calls), SimpleNamespace(id=800 + created.calls))
    created.calls = 0
    forum = SimpleNamespace(create_thread=created)
    thread = SimpleNamespace(id=701, guild_id=1, send=AsyncMock(return_value=SimpleNamespace(id=901)))
    bot = SimpleNamespace(wait_until_bridge_ready=AsyncMock(), fetch_forum_channel=AsyncMock(return_value=forum), get_thread_by_id=AsyncMock(return_value=thread))
    h = _handler(db, policy, bot=bot)
    await h.handle_post(_event(GROUP_A), runtime=None)
    await h.handle_post(_event(GROUP_B), runtime=None)
    await h.handle_comment(_event(GROUP_A, comment=True), runtime=None)
    nested = _event(GROUP_B, comment=True, parent=AUTHOR + '/objects/comments/1')
    nested.object.ap_id = AUTHOR + '/objects/comments/nested'
    result = await h.handle_comment(nested, runtime=None)
    assert result.status == 'skipped'
    assert thread.send.await_count == 1
    assert db.local_community_content.get_local_community_message_by_ap_object_id(nested.object.ap_id, local_community_id=groups[1].id) is None
    db.close()


@pytest.mark.asyncio
async def test_concurrent_reply_claim_blocks_second_send(tmp_path):
    """Two AP wrappers for one reply share its single in-row Discord attempt."""
    db, _, policy = _setup(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    async def reply_send(*_args, **_kwargs):
        reply_send.calls += 1
        started.set()
        await release.wait()
        return SimpleNamespace(id=903)
    reply_send.calls = 0
    forum = SimpleNamespace(create_thread=AsyncMock(return_value=(SimpleNamespace(id=701), SimpleNamespace(id=801))))
    thread = SimpleNamespace(id=701, guild_id=1, send=reply_send)
    bot = SimpleNamespace(wait_until_bridge_ready=AsyncMock(), fetch_forum_channel=AsyncMock(return_value=forum), get_thread_by_id=AsyncMock(return_value=thread))
    h = _handler(db, policy, bot=bot)
    await h.handle_post(_event(), runtime=None)
    first = asyncio.create_task(h.handle_comment(_event(comment=True, activity='reply/first'), runtime=None))
    await asyncio.wait_for(started.wait(), 4)
    second = await asyncio.wait_for(h.handle_comment(_event(comment=True, activity='reply/wrapper-2'), runtime=None), 4)
    assert second.status == 'skipped'
    assert reply_send.calls == 1
    release.set()
    assert (await first).status == 'processed'
    db.close()

@pytest.mark.asyncio
async def test_global_parent_mapping_fails_closed_after_second_group_placement(tmp_path, monkeypatch):
    """Gateway's old unscoped parent lookup must not select Group A over B."""
    import sys
    import types
    from fastapi import HTTPException, Response

    db, groups, policy = _setup(tmp_path)
    async def created(**_):
        created.calls += 1
        idx = created.calls
        return (SimpleNamespace(id=700 + idx), SimpleNamespace(id=800 + idx))
    created.calls = 0
    forum = SimpleNamespace(create_thread=created)
    bot = SimpleNamespace(
        wait_until_bridge_ready=AsyncMock(),
        fetch_forum_channel=AsyncMock(return_value=forum),
        get_thread_by_id=AsyncMock(side_effect=lambda thread_id:
            SimpleNamespace(id=thread_id, guild_id=1, send=AsyncMock(return_value=SimpleNamespace(id=900 + thread_id)))),
    )
    handler = _handler(db, policy, bot=bot)
    assert (await handler.handle_post(_event(GROUP_A), None)).status == 'processed'
    assert (await handler.handle_comment(_event(GROUP_A, comment=True), None)).status == 'processed'

    # Import the real FastAPI route with only the runtime class's type-only
    # import replaced; the SQLite/mapping/auth routes remain real code.
    fake_runtime_module = types.ModuleType('src.runtime')
    fake_runtime_module.Runtime = object
    monkeypatch.setitem(sys.modules, 'src.runtime', fake_runtime_module)
    from src.internal_fedify_api import create_internal_fedify_router, MessageMappingResolveRequest
    runtime = SimpleNamespace(database=db, settings=SimpleNamespace(fedify_shared_secret='cp21-secret'))
    router = create_internal_fedify_router(runtime)
    endpoint = next(route.endpoint for route in router.routes if route.path.endswith('/message-mappings/resolve'))
    request = MessageMappingResolveRequest(object_id=AUTHOR + '/objects/comments/1')
    good = endpoint(request, Response(), authorization='Bearer cp21-secret')
    assert good.community_actor_url == GROUP_A

    assert (await handler.handle_post(_event(GROUP_B), None)).status == 'processed'
    assert (await handler.handle_comment(_event(GROUP_B, comment=True), None)).status == 'processed'
    with pytest.raises(HTTPException) as exc:
        endpoint(request, Response(), authorization='Bearer cp21-secret')
    assert exc.value.status_code == 409
    assert db.local_community_content.list_delivered_local_group_message_tenants(request.object_id) == [
        groups[0].id, groups[1].id
    ]
    db.close()


def test_stale_sending_after_restart_becomes_unknown_without_new_claim(tmp_path):
    """Crash after a possible host send cannot mint a second Discord attempt."""
    from datetime import timedelta, timezone, datetime
    from src.models import LocalCommunityThread

    db, groups, _ = _setup(tmp_path)
    first = db.local_community_content.reserve_inbound_thread(
        local_community_id=groups[0].id, ap_activity_id=AUTHOR + '/activities/original',
        ap_object_id=ROOT, source_owner_actor_id=AUTHOR,
        origin_subscriber_actor_id=AUTHOR,
        inbound_create_snapshot={"title": "Original", "content": "original body"},
    )
    assert first.state == 'claimed' and first.attempt_id
    with db.session() as session:
        row = session.get(LocalCommunityThread, first.canonical.id)
        row.host_attempt_started_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    db.close()

    reopened = Database(f"sqlite:///{tmp_path / 'cp21.sqlite'}")
    next_attempt = reopened.local_community_content.reserve_inbound_thread(
        local_community_id=groups[0].id, ap_activity_id=AUTHOR + '/activities/duplicate',
        ap_object_id=ROOT, source_owner_actor_id=AUTHOR,
        origin_subscriber_actor_id=AUTHOR,
        inbound_create_snapshot={"title": "Replay", "content": "changed body"},
    )
    assert next_attempt.state == 'unknown' and next_attempt.attempt_id is None
    assert next_attempt.canonical.ap_activity_id == AUTHOR + '/activities/original'
    assert next_attempt.canonical.host_delivery_state == 'unknown'
    from src.db.repositories.local_community_content import HostIdentityConflict
    with pytest.raises(HostIdentityConflict):
        reopened.local_community_content.finish_inbound_host_attempt(
            canonical_id=first.canonical.id, attempt_id=first.attempt_id,
            kind='post', state='delivered', discord_thread_id=700, discord_message_id=800,
        )
    reopened.close()


@pytest.mark.asyncio
async def test_proven_pre_send_failure_can_retry_same_canonical_identity(tmp_path):
    """No Discord create_thread was called, so a later verified event may retry."""
    db, groups, policy = _setup(tmp_path)
    bot = SimpleNamespace(wait_until_bridge_ready=AsyncMock(), fetch_forum_channel=AsyncMock(side_effect=RuntimeError('unavailable')))
    h = _handler(db, policy, bot=bot)
    with pytest.raises(RuntimeError, match='unavailable'):
        await h.handle_post(_event(), runtime=None)
    first = db.local_community_content.get_local_community_thread_by_ap_object_id(ROOT, local_community_id=groups[0].id)
    assert first.host_delivery_state == 'failed'
    forum = SimpleNamespace(create_thread=AsyncMock(return_value=(SimpleNamespace(id=701), SimpleNamespace(id=801))))
    bot.fetch_forum_channel.side_effect = None
    bot.fetch_forum_channel.return_value = forum
    assert (await h.handle_post(_event(activity='announce/retry'), runtime=None)).status == 'processed'
    second = db.local_community_content.get_local_community_thread_by_ap_object_id(ROOT, local_community_id=groups[0].id)
    assert second.id == first.id and second.ap_activity_id == first.ap_activity_id
    assert second.host_delivery_state == 'delivered'
    forum.create_thread.assert_awaited_once()
    db.close()


def test_generic_inbound_ap_reference_never_suppresses_other_group_as_discord_echo(tmp_path):
    """A saved Group A AP reference must not skip valid inbound Group B content."""
    from src.activitypub_handlers import _is_discord_originated_echo

    db, _, _ = _setup(tmp_path)
    db.message_mappings.create_message_mapping(
        source_platform='activitypub', source_id=ROOT,
        activity_id='https://remote.example/activities/create/1', object_id=ROOT,
        actor_url=AUTHOR, community_actor_url=GROUP_A,
        discord_channel_id=701, discord_message_id=801,
    )
    runtime = SimpleNamespace(database=db)
    assert not _is_discord_originated_echo(_event(GROUP_A), runtime)
    assert not _is_discord_originated_echo(_event(GROUP_B), runtime)

    outbound = ROOT + '/discord-published'
    db.message_mappings.create_message_mapping(
        source_platform='discord', source_id='700/800',
        activity_id='https://remote.example/activities/discord-1', object_id=outbound,
        actor_url=AUTHOR, community_actor_url=GROUP_A,
        discord_channel_id=702, discord_message_id=802,
    )
    same = _event(GROUP_A)
    same.object.ap_id = outbound
    other = _event(GROUP_B)
    other.object.ap_id = outbound
    assert _is_discord_originated_echo(same, runtime)
    assert not _is_discord_originated_echo(other, runtime)
    db.close()


def test_revoked_subscriber_cannot_reserve_host_after_verification(tmp_path):
    """A signed Undo between HTTP actor proof and SQLite claim wins atomically."""
    from src.db.repositories.local_community_content import HostIdentityConflict
    db, groups, _ = _setup(tmp_path)
    assert db.remote_subscribers.delete_accepted_if_current_follow(
        local_community_id=groups[0].id, remote_actor_id=AUTHOR,
        follow_activity_id='https://remote.example/follow/1',
    )
    with pytest.raises(HostIdentityConflict, match='revoked'):
        db.local_community_content.reserve_inbound_thread(
            local_community_id=groups[0].id, ap_activity_id=AUTHOR + '/activities/create',
            ap_object_id=ROOT, source_owner_actor_id=AUTHOR,
            origin_subscriber_actor_id=AUTHOR,
            inbound_create_snapshot={"title": "Original", "content": "body"},
        )
    assert db.local_community_content.get_local_community_thread_by_ap_object_id(ROOT, local_community_id=groups[0].id) is None
    # Revoking A never revokes the same remote actor's independent B grant.
    valid = db.local_community_content.reserve_inbound_thread(
        local_community_id=groups[1].id, ap_activity_id=AUTHOR + '/activities/create',
        ap_object_id=ROOT, source_owner_actor_id=AUTHOR,
        origin_subscriber_actor_id=AUTHOR,
        inbound_create_snapshot={"title": "Original", "content": "body"},
    )
    assert valid.state == 'claimed'
    db.close()

@pytest.mark.asyncio
async def test_replayed_host_post_uses_original_content_for_new_sibling(tmp_path):
    """A later same-owner Create with changed body cannot rewrite a new sibling."""
    db, _, policy = _setup(tmp_path)
    forum = SimpleNamespace(create_thread=AsyncMock(return_value=(SimpleNamespace(id=701), SimpleNamespace(id=801))))
    bot = SimpleNamespace(wait_until_bridge_ready=AsyncMock(), fetch_forum_channel=AsyncMock(return_value=forum))
    handler = _handler(db, policy, bot=bot)
    first = _event()
    assert (await handler.handle_post(first, None)).status == "processed"
    original = handler.surface_fanout.fanout_thread_to_local_subscribers.await_args.kwargs
    later_json = _event(activity="announce/replay").model_dump(mode="json")
    later_json["object"]["title"] = "Changed by replay"
    later_json["object"]["body_markdown"] = "replacement injected text"
    later = ActivityPubEvent.model_validate(later_json)
    await handler.handle_post(later, None)
    mirrored = handler.surface_fanout.fanout_thread_to_local_subscribers.await_args.kwargs
    assert mirrored["title"] == original["title"]
    assert mirrored["content"] == original["content"]
    forum.create_thread.assert_awaited_once()
    db.close()


@pytest.mark.asyncio
async def test_replayed_host_comment_uses_original_content_for_new_sibling(tmp_path):
    """One tenant reply body is frozen at first reservation across duplicate AP IDs."""
    db, _, policy = _setup(tmp_path)
    forum = SimpleNamespace(create_thread=AsyncMock(return_value=(SimpleNamespace(id=701), SimpleNamespace(id=801))))
    thread = SimpleNamespace(id=701, guild_id=1, send=AsyncMock(return_value=SimpleNamespace(id=802)))
    bot = SimpleNamespace(wait_until_bridge_ready=AsyncMock(), fetch_forum_channel=AsyncMock(return_value=forum), get_thread_by_id=AsyncMock(return_value=thread))
    handler = _handler(db, policy, bot=bot)
    assert (await handler.handle_post(_event(), None)).status == "processed"
    assert (await handler.handle_comment(_event(comment=True), None)).status == "processed"
    original = handler.surface_fanout.fanout_message_to_local_subscribers.await_args.kwargs
    later_json = _event(activity="announce/replayed-reply", comment=True).model_dump(mode="json")
    later_json["object"]["body_markdown"] = "replacement injected reply"
    later = ActivityPubEvent.model_validate(later_json)
    await handler.handle_comment(later, None)
    mirrored = handler.surface_fanout.fanout_message_to_local_subscribers.await_args.kwargs
    assert mirrored["content"] == original["content"]
    thread.send.assert_awaited_once()
    db.close()

@pytest.mark.asyncio
async def test_pre_send_failure_and_restart_keep_original_host_body(tmp_path):
    """Provably unsent first Create retries after restart with its first snapshot."""
    db, groups, policy = _setup(tmp_path)
    first_bot = SimpleNamespace(wait_until_bridge_ready=AsyncMock(),
                                fetch_forum_channel=AsyncMock(side_effect=RuntimeError("pre-send failure")))
    first_handler = _handler(db, policy, bot=first_bot)
    with pytest.raises(RuntimeError, match="pre-send failure"):
        await first_handler.handle_post(_event(), None)
    saved = db.local_community_content.get_local_community_thread_by_ap_object_id(
        ROOT, local_community_id=groups[0].id,
    )
    assert saved.host_delivery_state == "failed"
    original_body = saved.inbound_create_snapshot_json["content"]
    db.close()

    reopened = Database(f"sqlite:///{tmp_path / 'cp21.sqlite'}")
    forum = SimpleNamespace(create_thread=AsyncMock(return_value=(SimpleNamespace(id=701), SimpleNamespace(id=801))))
    bot = SimpleNamespace(wait_until_bridge_ready=AsyncMock(), fetch_forum_channel=AsyncMock(return_value=forum))
    retry_handler = _handler(reopened, policy, bot=bot)
    retry_json = _event(activity="announce/retry").model_dump(mode="json")
    retry_json["object"]["body_markdown"] = "replaced on retry"
    result = await retry_handler.handle_post(ActivityPubEvent.model_validate(retry_json), None)
    assert result.status == "processed"
    assert forum.create_thread.await_args.kwargs["content"] == original_body
    reopened.close()


@pytest.mark.asyncio
async def test_legacy_host_without_original_snapshot_fails_closed_on_replay(tmp_path):
    """A historical completed host without first-body evidence cannot gain new mirrors."""
    from sqlalchemy import select
    from src.models import LocalCommunityThread

    db, groups, policy = _setup(tmp_path)
    forum = SimpleNamespace(create_thread=AsyncMock(return_value=(SimpleNamespace(id=701), SimpleNamespace(id=801))))
    bot = SimpleNamespace(wait_until_bridge_ready=AsyncMock(), fetch_forum_channel=AsyncMock(return_value=forum))
    handler = _handler(db, policy, bot=bot)
    await handler.handle_post(_event(), None)
    with db.session() as session:
        saved = session.scalar(select(LocalCommunityThread).where(
            LocalCommunityThread.local_community_id == groups[0].id,
            LocalCommunityThread.ap_object_id == ROOT,
        ))
        saved.inbound_create_snapshot_json = None  # v0 migration has no body evidence.
    handler.surface_fanout.reset_mock()
    replay = _event(activity="announce/unprovable")
    outcome = await handler.handle_post(replay, None)
    assert outcome.status == "skipped"
    handler.surface_fanout.fanout_thread_to_local_subscribers.assert_not_awaited()
    forum.create_thread.assert_awaited_once()
    db.close()
