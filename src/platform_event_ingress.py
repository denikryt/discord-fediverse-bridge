"""Single connector-scoped normalized Gateway mutation ingress.

Stage 03 routes edits/deletes, Personal Feed roots/replies and Community
forum starters/replies through one connector-scoped sink.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from .platform_types import (
    MessageCreated, MessageDeleted, MessageUpdated, PlatformInboundEvent,
    PlatformOperationError, ThreadCreated, ThreadDeleted, ThreadUpdated,
)

class PlatformCreateRouter(Protocol):
    """Consume neutral authored Create events through existing domain services."""

    async def handle_platform_message_create(self, event: MessageCreated) -> object:
        """Route a trusted, typed root or reply to the owning canonical publisher."""

    async def handle_platform_thread_create(self, event: ThreadCreated) -> object:
        """Adopt an existing Personal Feed root or route a verified forum starter."""


if TYPE_CHECKING:
    from .runtime_ports import PlatformAdapterPort
    from .discord_message_mutations import DiscordMessageMutationHandler

logger = logging.getLogger(__name__)


class PlatformEventIngress:
    """Admit connector-verified Gateway notices into exactly one domain route.

    Normalized message mutation behavior stays with the existing mutation
    handler; thread metadata events have no canonical AP mutation owner in
    Stage 03. Forum Create paths dispatch to authorized canonical runtimes; unknown
    or incomplete events fail closed instead of entering a legacy SDK route.
    """

    def __init__(
        self, *, adapter: PlatformAdapterPort,
        mutations: DiscordMessageMutationHandler,
        creates: PlatformCreateRouter | None = None,
    ) -> None:
        """Bind one physical connector to one owner of message mutation effects."""
        self.adapter = adapter
        self.mutations = mutations
        self.creates = creates

    async def handle_platform_event(self, event: PlatformInboundEvent) -> None:
        """Reject foreign events before routing or reading persisted mappings."""
        if not isinstance(event, (
            MessageCreated, MessageUpdated, MessageDeleted,
            ThreadCreated, ThreadUpdated, ThreadDeleted,
        )):
            raise TypeError('unsupported normalized Gateway event')
        if event.connector_key != self.adapter.connector.key:
            raise PlatformOperationError('invalid_request', 'Gateway event connector mismatch')

        if isinstance(event, MessageUpdated):
            await self.mutations.handle_platform_message_edit(event, self.adapter)
        elif isinstance(event, MessageDeleted):
            await self.mutations.handle_platform_message_delete(event, self.adapter)
        elif isinstance(event, (ThreadUpdated, ThreadDeleted)):
            # MULTIPLATFORM_STAGE03_TEMP_THREAD_MUTATIONS: Physical thread update/delete notices are normalized but not canonical AP mutations; Stage 04 must resolve source/copy ownership before enabling any propagation.
            # No existing domain runtime owns a physical thread rename/delete
            # as an AP mutation. Keep this an observable no-op, never an edit.
            logger.info(
                'Gateway %s for thread %s has no canonical mutation owner',
                type(event).__name__, event.thread.external_thread_id,
            )
        elif isinstance(event, MessageCreated) and self.creates is not None:
            await self.creates.handle_platform_message_create(event)
        elif isinstance(event, ThreadCreated) and self.creates is not None:
            await self.creates.handle_platform_thread_create(event)
        else:
            # A missing domain Create binding must never silently acknowledge
            # a community post or reply that was not published.
            raise RuntimeError('normalized create ingress is not bound')
