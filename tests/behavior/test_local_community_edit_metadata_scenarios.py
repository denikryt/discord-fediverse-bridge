"""Behavior scenarios for local-community metadata editing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.local_communities.service import LocalCommunityService
from src.operations import EditCommunityInput, edit_community_operation
from support.db import build_database


def _settings(*, super_admins: list[str] | None = None) -> SimpleNamespace:
    """Build settings with only the fields metadata edit behavior uses."""
    return SimpleNamespace(discord_guild_allowlist=[], local_community_operator_allowlist=super_admins or [])


def test_user_action_edits_local_community_metadata_end_to_end(tmp_path: Path) -> None:
    """Owner modal submit updates the same DB row future readers load."""
    database = build_database(tmp_path, "edit-metadata-end-to-end.db")
    LocalCommunityService(
        database=database,
        base_url="https://bridge.example",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=100,
        slug="cats",
        name="Cats",
        description="Old summary",
        created_by_discord_user_id="111",
    )

    result = edit_community_operation(
        EditCommunityInput(
            database=database,
            settings=_settings(),
            discord_user_id="111",
            discord_guild_id=10,
            community_slug="cats",
            display_name="Updated Cats",
            summary="Updated summary",
        )
    )
    reloaded = database.local_communities.get_local_community_by_slug("cats")

    assert result.applied is True
    assert reloaded.display_name == "Updated Cats"
    assert reloaded.summary == "Updated summary"
    assert reloaded.slug == "cats"
    assert reloaded.discord_forum_channel_id == 100
    assert reloaded.actor_url == "https://bridge.example/communities/cats"


def test_local_only_metadata_edit_does_not_touch_moderation_or_identity(tmp_path: Path) -> None:
    """Editing metadata must not mutate unrelated community identity or bans."""
    database = build_database(tmp_path, "edit-metadata-local-only.db")
    LocalCommunityService(
        database=database,
        base_url="https://bridge.example",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=100,
        slug="cats",
        name="Cats",
        description="Old summary",
        created_by_discord_user_id="111",
    )
    community = database.local_communities.get_local_community_by_slug("cats")
    database.community_actor_bans.create_active_ban(
        local_community_id=community.id,
        actor_handle="alice@example.com",
        actor_url=None,
        created_by_discord_user_id="111",
        reason="spam",
    )

    edit_community_operation(
        EditCommunityInput(
            database=database,
            settings=_settings(),
            discord_user_id="111",
            discord_guild_id=10,
            community_slug="cats",
            display_name="Cats",
            summary=None,
        )
    )
    reloaded = database.local_communities.get_local_community_by_slug("cats")
    ban = database.community_actor_bans.get_active_ban_by_handle(
        local_community_id=community.id,
        actor_handle="alice@example.com",
    )

    assert reloaded.summary is None
    assert reloaded.public_key_pem == "public-key"
    assert reloaded.private_key_pem == "private-key"
    assert ban is not None
    assert ban.reason == "spam"
