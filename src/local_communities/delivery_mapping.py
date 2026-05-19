"""Lookup helpers for local-community thread and message mappings.

These helpers keep `LocalCommunityRuntime` focused on orchestration while
centralising the canonical DB lookups that define thread/message ownership in
the local-community mode.
"""

from __future__ import annotations

from ..db import Database


def get_local_community_for_forum(database: Database, forum_channel_id: int) -> object | None:
    """Return the local community bound to one Discord forum channel, if any."""
    return database.get_local_community_by_forum_channel_id(forum_channel_id)


def get_local_community_thread_for_discord_thread(
    database: Database, discord_thread_id: int
) -> object | None:
    """Return the canonical local-community thread row for one Discord thread."""
    return database.get_local_community_thread_by_discord_thread_id(discord_thread_id)


def get_local_community_thread_for_ap_object(
    database: Database, ap_object_id: str
) -> object | None:
    """Return the canonical local-community thread row for one AP post object."""
    return database.get_local_community_thread_by_ap_object_id(ap_object_id)


def get_local_community_message_for_discord_message(
    database: Database, discord_message_id: int
) -> object | None:
    """Return the local-community message row for one Discord message, if any."""
    return database.get_local_community_message_by_discord_message_id(discord_message_id)


def get_local_community_message_for_ap_object(
    database: Database, ap_object_id: str
) -> object | None:
    """Return the local-community message row for one AP comment object, if any."""
    return database.get_local_community_message_by_ap_object_id(ap_object_id)
