"""Bidirectional Community replies through the one verified normalized ingress.

A human can author a reply in a source or mirrored forum; that creates exactly
one AP object and only the other eligible physical destinations receive copies.
Bridge-owned messages, replay and false parent references cannot loop back.
"""
from __future__ import annotations

from dataclasses import replace
import pytest

from src.platform_types import AuthorRef, PlatformOperationError
from test_normalized_community_runtime import reply, starter, world


async def _created_world(tmp_path, *, forum_ids=(100, 101)):
    """Create one canonical source post and real per-forum starter receipts."""
    db, runtime, gateway, transport = world(tmp_path, forum_ids=forum_ids)
    event, observed = starter()
    outcome = await runtime.handle_normalized_thread_create(event, observed, adapter=transport)
    assert outcome.status == 'published'
    return db, runtime, gateway, transport, outcome


@pytest.mark.asyncio
async def test_source_thread_message_publishes_to_ap_and_mirrors(tmp_path):
    """A root-forum human reply creates one AP Note and one sibling message."""
    db, runtime, gateway, adapter, post = await _created_world(tmp_path)
    try:
        result = await runtime.handle_normalized_message_create(reply(), adapter=adapter)
        assert result.status == 'published' and len(gateway.requests) == 2
        assert [(dst.external_thread_id, ref.external_message_id)
                for dst, ref, _attempt in adapter.replies] == [('1101', '2101')]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_mirror_thread_message_publishes_to_ap_and_fanout(tmp_path):
    """An authored reply in mirror B is forwarded to original forum A, not B."""
    db, runtime, gateway, adapter, post = await _created_world(tmp_path)
    try:
        result = await runtime.handle_normalized_message_create(
            reply(forum='101', thread='1101', message='601', parent='2101'), adapter=adapter,
        )
        assert result.status == 'published' and len(gateway.requests) == 2
        assert [dst.external_thread_id for dst, _ref, _attempt in adapter.replies] == ['200']
    finally:
        db.close()


@pytest.mark.asyncio
async def test_mirror_thread_message_fans_out_to_all_other_mirrors(tmp_path):
    """B→A/C fanout excludes B and persists one independent receipt per target."""
    db, runtime, gateway, adapter, post = await _created_world(tmp_path, forum_ids=(100, 101, 102))
    try:
        result = await runtime.handle_normalized_message_create(
            reply(forum='101', thread='1101', message='601', parent='2101'), adapter=adapter,
        )
        group = db.discord_fanout_groups.get_message_group_by_source_message(601)
        assert result.status == 'published' and group is not None
        assert {dst.external_thread_id for dst, _ref, _attempt in adapter.replies} == {'200', '1102'}
        copies = db.discord_fanout_groups.get_message_deliveries(group.id)
        assert {copy.external_thread_id for copy in copies} == {'200', '1101', '1102'}
        assert len(gateway.requests) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_reply_to_message_in_mirror_thread_resolves_reference(tmp_path):
    """B's nested child references B's original message and A's parent copy."""
    db, runtime, gateway, adapter, post = await _created_world(tmp_path)
    try:
        await runtime.handle_normalized_message_create(
            reply(forum='101', thread='1101', message='601', parent='2101'), adapter=adapter,
        )
        parent_group = db.discord_fanout_groups.get_message_group_by_source_message(601)
        child = await runtime.handle_normalized_message_create(
            reply(forum='101', thread='1101', message='602', parent='601'), adapter=adapter,
        )
        child_group = db.discord_fanout_groups.get_message_group_by_source_message(602)
        assert child.status == 'published' and child_group.parent_message_group_id == parent_group.id
        assert [dst.external_thread_id for dst, _ref, _attempt in adapter.replies] == ['200', '200']
        assert adapter.replies[1][1].external_message_id == '3200'
        assert gateway.requests[-1].in_reply_to_object_id == gateway.requests[-2].object_id
    finally:
        db.close()


@pytest.mark.asyncio
async def test_reply_to_mirror_starter_is_treated_as_root_reply(tmp_path):
    """A reply to B's starter targets the original AP post, not a comment."""
    db, runtime, gateway, adapter, post = await _created_world(tmp_path)
    try:
        created = await runtime.handle_normalized_message_create(
            reply(forum='101', thread='1101', message='602', parent='2101'), adapter=adapter,
        )
        group = db.discord_fanout_groups.get_message_group_by_source_message(602)
        assert created.status == 'published' and group.parent_message_group_id is None
        assert gateway.requests[-1].in_reply_to_object_id == post.object_id
        assert adapter.replies[-1][1].external_message_id == '300'
    finally:
        db.close()


@pytest.mark.asyncio
async def test_bot_message_in_mirror_thread_is_not_re_published(tmp_path):
    """An untrusted bot author is rejected before AP render or physical fanout."""
    db, runtime, gateway, adapter, post = await _created_world(tmp_path)
    try:
        mirrored = reply(forum='101', thread='1101', message='601', parent='2101')
        forged = replace(mirrored, author=AuthorRef('discord-primary','999999','bot','Bridge'))
        with pytest.raises(PlatformOperationError):
            await runtime.handle_normalized_message_create(forged, adapter=adapter)
        assert len(gateway.requests) == 1 and adapter.replies == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_mirror_message_does_not_loop_back_via_fanout(tmp_path):
    """Mirrored physical receipts never authorize a new AP Create on bot echo."""
    db, runtime, gateway, adapter, post = await _created_world(tmp_path)
    try:
        message = reply(forum='101', thread='1101', message='601', parent='2101')
        await runtime.handle_normalized_message_create(message, adapter=adapter)
        assert [dst.external_thread_id for dst, _ref, _attempt in adapter.replies] == ['200']
        # The confirmed copy is received as a new message event from the
        # physical destination; its typed receipt suppresses publication.
        echo = reply(forum='100', thread='200', message='3200', parent='300')
        second = await runtime.handle_normalized_message_create(echo, adapter=adapter)
        assert second.status == 'ignored' and second.reason == 'duplicate_discord_message'
        assert len(gateway.requests) == 2 and len(adapter.replies) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_mirror_message_dedup_on_reconnect(tmp_path):
    """Repeated verified B message is one canonical Note and one A receipt."""
    db, runtime, gateway, adapter, post = await _created_world(tmp_path)
    try:
        message = reply(forum='101', thread='1101', message='601', parent='2101')
        one = await runtime.handle_normalized_message_create(message, adapter=adapter)
        two = await runtime.handle_normalized_message_create(message, adapter=adapter)
        assert one.status == 'published' and two.status == 'ignored'
        assert len(gateway.requests) == 2 and len(adapter.replies) == 1
        assert db.discord_fanout_groups.get_message_group_by_source_message(601) is not None
    finally:
        db.close()
