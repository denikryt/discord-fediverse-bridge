"""Checked projection of Discord-only physical identities into legacy SQLite IDs.

Discord adapter integration preserves preexisting integer Discord repository columns, not a new
multi-platform identifier scheme. Only a verified discord-primary reference may
cross this narrow boundary; durable delivery replaces these lookups with scoped keys.
"""
from __future__ import annotations

from typing import Literal

from .platform_types import (
    AuthorRef, ChannelRef, MessageRef, PlatformOperationError, ThreadRef,
)


def discord_id_to_legacy_int(
    ref: ChannelRef | ThreadRef | MessageRef | AuthorRef,
    *, component: Literal['id', 'parent', 'container'] = 'id',
) -> int:
    """Project one named physical Discord snowflake into a legacy integer key.

    Thread parents and immediate message containers require an explicit selector
    so downstream persistence never substitutes the thread or starter ID.
    """
    if not isinstance(ref, (ChannelRef, ThreadRef, MessageRef, AuthorRef)):
        raise PlatformOperationError('invalid_request', 'expected platform reference')
    if ref.connector_key != 'discord-primary':
        raise PlatformOperationError('invalid_request', 'legacy Discord connector mismatch')
    if isinstance(ref, ThreadRef):
        if component == 'parent':
            raw = ref.channel.external_channel_id
        elif component == 'id':
            raw = ref.external_thread_id
        else:
            raise PlatformOperationError('invalid_request', 'unsupported reference component')
    elif isinstance(ref, MessageRef):
        if component == 'container':
            raw = ref.external_container_id
        elif component == 'id':
            raw = ref.external_message_id
        else:
            raise PlatformOperationError('invalid_request', 'unsupported reference component')
    elif component != 'id':
        raise PlatformOperationError('invalid_request', 'unsupported reference component')
    elif isinstance(ref, ChannelRef):
        raw = ref.external_channel_id
    else:
        raw = ref.external_actor_id
    # MULTIPLATFORM_DISCORD_ADAPTER_TEMP_LEGACY_IDS: Convert only verified discord-primary refs into legacy integer repository projections; durable delivery must use connector-scoped source/copy lookups and delete this shim.
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal() or int(raw) <= 0:
        raise PlatformOperationError('invalid_request', 'invalid Discord legacy snowflake')
    return int(raw)
