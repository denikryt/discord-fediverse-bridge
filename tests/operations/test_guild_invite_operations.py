"""Contract tests for declarative guild invite operations."""

from __future__ import annotations

from src.operations.publish_guild_invite import publish_guild_invite_operation
from src.operations.remove_guild_invite import remove_guild_invite_operation


def test_publish_operation_declares_eligibility_in_execution_order() -> None:
    """Publish eligibility remains visible as ordered DiscordOps preconditions."""
    assert tuple(condition.name for condition in publish_guild_invite_operation.preconditions) == (
        "no_active_local_community",
        "no_invitable_local_community_channel",
    )


def test_remove_operation_declares_published_invite_requirement() -> None:
    """Removal declares the current-publication requirement through DiscordOps."""
    assert tuple(condition.name for condition in remove_guild_invite_operation.preconditions) == (
        "guild_invite_not_published",
    )
