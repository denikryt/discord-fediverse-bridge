"""Behavior tests for guild invite publication and dashboard state."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.db import Database
from src.dashboard import build_dashboard_payload
from src.operations.publish_guild_invite import PublishGuildInviteInput, run_publish_guild_invite
from src.operations.remove_guild_invite import RemoveGuildInviteInput, run_remove_guild_invite


def _create_active_community(
    database: Database,
    *,
    guild_id: int = 10,
    channel_id: int = 20,
    slug: str = "cats",
) -> None:
    """Create one active local community for invite eligibility tests."""
    database.local_communities.create_local_community(
        discord_guild_id=guild_id,
        discord_forum_channel_id=channel_id,
        slug=slug,
        display_name=slug.title(),
        summary=None,
        created_by_discord_user_id="1",
        actor_url=f"https://bridge.example/c/{slug}",
        inbox_url=f"https://bridge.example/c/{slug}/inbox",
        outbox_url=f"https://bridge.example/c/{slug}/outbox",
        followers_url=f"https://bridge.example/c/{slug}/followers",
        public_key_pem="public",
        private_key_pem="private",
        status="active",
    )


def _guild(*, guild_id: int = 10) -> object:
    """Build a fake Discord guild with a mutable channel cache."""
    channels: dict[int, object] = {}
    return SimpleNamespace(
        id=guild_id,
        me=object(),
        channels=channels,
        get_channel=lambda channel_id: channels.get(channel_id),
    )


def _channel(
    *,
    guild: object,
    channel_id: int = 20,
    invite: object | None = None,
    can_create_invite: bool = True,
) -> object:
    """Build an invite-capable fake Discord channel and add it to the guild cache."""

    channel = SimpleNamespace(
        id=channel_id,
        name="cats",
        guild=guild,
        permissions_for=lambda member: SimpleNamespace(
            create_instant_invite=can_create_invite
        ),
        create_invite=AsyncMock(
            return_value=invite
            or SimpleNamespace(
                code="new",
                url="https://discord.gg/new",
                delete=AsyncMock(),
            )
        ),
    )
    guild.channels[channel_id] = channel
    return channel


@pytest.mark.asyncio
async def test_publish_and_remove_guild_invite(tmp_path) -> None:
    """A valid host channel publishes one invite and removal clears it."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    _create_active_community(database)
    guild = _guild()
    channel = _channel(guild=guild)
    fetched = SimpleNamespace(delete=AsyncMock())
    client = SimpleNamespace(fetch_invite=AsyncMock(return_value=fetched))

    published = await run_publish_guild_invite(
        PublishGuildInviteInput(
            database=database,
            client=client,
            guild=guild,
            actor_discord_user_id="42",
        )
    )
    assert published.reason == "published"
    assert database.guild_invite_publications.get_by_guild_id(10).invite_url == "https://discord.gg/new"
    channel.create_invite.assert_awaited_once_with(
        max_age=0,
        max_uses=0,
        unique=True,
        reason="Published on bridge dashboard",
    )

    removed = await run_remove_guild_invite(
        RemoveGuildInviteInput(
            database=database,
            client=client,
            guild=guild,
            actor_discord_user_id="42",
        )
    )
    assert removed.reason == "removed"
    assert database.guild_invite_publications.get_by_guild_id(10) is None
    fetched.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_publication_requires_active_local_community(tmp_path) -> None:
    """Invite creation is side-effect free without an active local community."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    guild = _guild()
    channel = _channel(guild=guild)

    result = await run_publish_guild_invite(
        PublishGuildInviteInput(
            database=database,
            client=SimpleNamespace(),
            guild=guild,
            actor_discord_user_id="42",
        )
    )

    assert result.reason == "no_active_local_community"
    channel.create_invite.assert_not_awaited()


@pytest.mark.asyncio
async def test_publication_selects_first_invitable_active_host_channel(tmp_path) -> None:
    """The operation skips unusable hosts and selects the first invitable one."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    _create_active_community(database, channel_id=20, slug="alpha")
    _create_active_community(database, channel_id=21, slug="beta")
    guild = _guild()
    blocked = _channel(guild=guild, channel_id=20, can_create_invite=False)
    selected = _channel(guild=guild, channel_id=21)

    result = await run_publish_guild_invite(
        PublishGuildInviteInput(
            database=database,
            client=SimpleNamespace(),
            guild=guild,
            actor_discord_user_id="42",
        )
    )

    assert result.reason == "published"
    blocked.create_invite.assert_not_awaited()
    selected.create_invite.assert_awaited_once()
    assert database.guild_invite_publications.get_by_guild_id(10).discord_channel_id == 21


@pytest.mark.asyncio
async def test_publication_rejects_when_no_active_host_channel_can_create_invite(tmp_path) -> None:
    """An active community is insufficient when none of its channels can invite."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    _create_active_community(database)
    guild = _guild()
    channel = _channel(guild=guild, can_create_invite=False)

    result = await run_publish_guild_invite(
        PublishGuildInviteInput(
            database=database,
            client=SimpleNamespace(),
            guild=guild,
            actor_discord_user_id="42",
        )
    )

    assert result.reason == "no_invitable_local_community_channel"
    channel.create_invite.assert_not_awaited()


def test_dashboard_attaches_invite_only_to_existing_guild_bucket(tmp_path) -> None:
    """Published invites augment existing guild cards without creating orphans."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    _create_active_community(database)
    with database.session() as session:
        database.guild_invite_publications.replace_in_session(
            session,
            discord_guild_id=10,
            discord_channel_id=20,
            invite_code="abc",
            invite_url="https://discord.gg/abc",
            published_by_discord_user_id="42",
        )
        database.guild_invite_publications.replace_in_session(
            session,
            discord_guild_id=999,
            discord_channel_id=998,
            invite_code="orphan",
            invite_url="https://discord.gg/orphan",
            published_by_discord_user_id="42",
        )
    runtime = SimpleNamespace(
        database=database,
        settings=SimpleNamespace(
            fedify_origin="https://bridge.example",
            normalized_fedify_origin="https://bridge.example",
            fedify_actor_identifier="bridge",
            federation_allowlist=[],
        ),
    )
    payload = build_dashboard_payload(runtime)
    assert len(payload["discordGuilds"]) == 1
    assert payload["discordGuilds"][0]["inviteUrl"] == "https://discord.gg/abc"


@pytest.mark.asyncio
async def test_replacement_stays_published_when_old_invite_cleanup_fails(tmp_path) -> None:
    """Old-invite cleanup is best-effort after the new row commits."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    _create_active_community(database)
    guild = _guild()
    first_channel = _channel(
        guild=guild,
        invite=SimpleNamespace(
            code="first",
            url="https://discord.gg/first",
            delete=AsyncMock(),
        ),
    )
    client = SimpleNamespace(fetch_invite=AsyncMock(side_effect=RuntimeError("cleanup failed")))

    await run_publish_guild_invite(
        PublishGuildInviteInput(
            database=database,
            client=client,
            guild=guild,
            actor_discord_user_id="42",
        )
    )

    first_channel.create_invite = AsyncMock(
        return_value=SimpleNamespace(
            code="second",
            url="https://discord.gg/second",
            delete=AsyncMock(),
        )
    )
    result = await run_publish_guild_invite(
        PublishGuildInviteInput(
            database=database,
            client=client,
            guild=guild,
            actor_discord_user_id="42",
        )
    )

    assert result.reason == "replaced"
    assert database.guild_invite_publications.get_by_guild_id(10).invite_code == "second"
    actions = [row.action for row in database.management_audit_events.list_oldest_first()]
    assert actions == ["guild_invite.published", "guild_invite.replaced"]
