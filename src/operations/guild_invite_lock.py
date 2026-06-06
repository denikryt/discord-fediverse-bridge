"""Shared per-guild serialization for guild invite operations."""

from __future__ import annotations

import asyncio

_GUILD_LOCKS: dict[int, asyncio.Lock] = {}


def guild_invite_lock(guild_id: int) -> asyncio.Lock:
    """Return the shared lock protecting invite state for one Discord guild."""
    # Publish and remove must use the same registry so their Discord and DB
    # side effects cannot interleave for one guild.
    return _GUILD_LOCKS.setdefault(guild_id, asyncio.Lock())
