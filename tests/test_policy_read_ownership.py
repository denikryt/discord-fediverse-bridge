"""Structural coverage for Stage 7 policy-read ownership boundaries."""

from __future__ import annotations

import inspect

from src import activitypub_handlers
from src.community_sync.discord_fanout import DiscordFanout
from src.discord_event_router import DiscordEventRouter
from src.local_communities.discord_fanout import LocalCommunityDiscordFanout


def test_single_question_runtime_boundaries_use_narrow_policy_evaluators() -> None:
    """Lower runtime boundaries do not choose snapshots for one policy question."""
    sources = (
        inspect.getsource(DiscordEventRouter._is_guild_allowed),
        inspect.getsource(DiscordFanout._channel_is_allowed),
        inspect.getsource(LocalCommunityDiscordFanout._surface_is_allowed),
        inspect.getsource(LocalCommunityDiscordFanout._target_is_allowed),
        inspect.getsource(activitypub_handlers.dispatch_activitypub_event),
    )
    for source in sources:
        assert ".snapshot()" not in source
