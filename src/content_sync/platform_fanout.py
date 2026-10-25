"""Connector-neutral physical Create coordinator over durable SQLite intents.

An adapter is selected by its exact connector. A physical POST may happen only
once for a committed claim; unknown responses stay unresolved until read-only
exact-attempt receipt adoption, never an automatic retry.
"""
from __future__ import annotations

import logging

from ..platform_types import PlatformOperationError
from ..db.repositories.platform_delivery_intents import decode_request

logger=logging.getLogger(__name__)


class PlatformFanoutCoordinator:
    """Serialize recipient Create through durable attempts and typed adapters."""

    def __init__(self, database, registry):
        """Use a Database writer and connector-keyed adapter registry."""
        self.database=database
        self.registry=registry

    @classmethod
    def from_adapter(cls, database, adapter):
        """Build a strict one-connector registry for the adapter already in runtime.

        The adapter must implement the complete lookup and delivery contract;
        accepting an arbitrary send-only object would bypass registry identity.
        """
        from ..platform_adapters import PlatformAdapterRegistry
        registry=PlatformAdapterRegistry((adapter.connector,))
        registry.register(adapter)
        return cls(database,registry)

    async def stage_and_deliver(self, *,domain,kind,canonical_id,forum,
                                operation,destination,content,presentation,
                                title=None,reply_to=None):
        """Authorize persisted target, freeze DTO, and submit one claimed send."""
        placement=self.database.platform_surfaces.resolve_claim(
            forum.connector_key,forum.external_channel_id)
        if (placement is None or placement.kind!='forum'
            or placement.external_guild_id!=forum.external_guild_id
            or placement.connector_key!=forum.connector_key):
            return False
        owner_map={'local_host':placement.local_community_id,
                   'local_subscriber':placement.local_subscriber_id,
                   'remote_subscription':placement.remote_subscription_id}
        owner_id=owner_map.get(placement.role)
        if owner_id is None:return False
        key=f'fanout:v1:{domain}:{kind}:{canonical_id}:{forum.connector_key}:{placement.id}'
        from ..platform_types import CreateAttempt
        attempt=CreateAttempt(key,forum.connector_key,operation,destination)
        row=self.database.platform_delivery_intents.reserve(
            domain=domain,kind=kind,canonical_id=canonical_id,forum=forum,
            channel_surface_id=placement.id,owner_kind=placement.role,
            owner_id=owner_id,attempt=attempt,content=content,
            presentation=presentation,title=title,reply_to=reply_to)
        return await self.deliver(row.id)

    async def recover_pending(self) -> list[int]:
        """Restart safe Create work without guessing whether old POSTs succeeded.

        Claims from an expired or interrupted transport are made unresolved,
        never pending. Only pristine persisted DTOs with a live canonical
        source and owner are dispatched through the ordinary claim path.
        """
        self.database.platform_delivery_intents.expire_claims()
        # Parent delivery and startup both consult the same authoritative
        # SQLite dependency evidence; a saved blocked row alone is not a send.
        self.database.platform_delivery_intents.materialize_ready_blocked()
        completed: list[int] = []
        for intent_id in self.database.platform_delivery_intents.recoverable_pending_ids():
            if await self.deliver(intent_id):
                completed.append(intent_id)
        return completed

    async def _wake_blocked_children(self) -> None:
        """Run newly provable nested deliveries, never unknown attempts.

        Nested children can themselves be parents. The reentrancy guard and
        bounded passes avoid unbounded AP-driven recursion without creating a
        separate queue; remaining ready SQLite rows survive later wake/restart.
        """
        if getattr(self, '_waking_children', False):
            return
        self._waking_children = True
        try:
            for _ in range(128):
                ready = self.database.platform_delivery_intents.materialize_ready_blocked()
                if not ready:
                    break
                for child_id in ready:
                    await self.deliver(child_id)
        finally:
            self._waking_children = False

    async def deliver(self,intent_id:int)->bool:
        """Send only a newly claimed immutable Create, commit verified receipt.

        Return False for ineligible/already handled/unknown attempts. The
        durable intent rather than this return value decides restart behavior.
        """
        claim=self.database.platform_delivery_intents.claim(intent_id)
        if claim is None:return False
        try:
            attempt,content,presentation,title,reply_to=decode_request(claim.request_json)
            adapter=self.registry.get(attempt.connector_key)
            # A valid claim may become ineligible during registry selection.
            # Re-read canonical deletion and the current surface owner at the
            # last authoritative pre-transport boundary, never from cache.
            if not self.database.platform_delivery_intents.dispatch_still_authorized(claim):
                self.database.platform_delivery_intents.mark_unresolved(
                    intent_id, token=claim.token, reason='pre_send_revoked')
                return False
            # No fallback to a Discord singleton or raw SDK path. Distinguish
            # the three operation types before constructing a network request.
            if attempt.operation=='forum_thread':
                receipt=await adapter.create_forum_thread(
                    attempt.destination,content,title=title,presentation=presentation,attempt=attempt)
            elif attempt.operation=='reply_thread':
                receipt=await adapter.create_reply_thread(attempt.destination,title=title,attempt=attempt)
            elif attempt.operation=='message':
                receipt=await adapter.send_message(
                    attempt.destination,content,reply_to=reply_to,
                    presentation=presentation,attempt=attempt)
            else:
                raise PlatformOperationError('invalid_request','unsupported physical create operation')
            # A successful return still requires exact typed receipt validation
            # at the repository boundary, inside the mapping transaction.
            accepted = self.database.platform_delivery_intents.commit_receipt(
                intent_id,token=claim.token,receipt=receipt)
            if accepted:
                # A confirmed physical comment becomes the exact prerequisite
                # for any children waiting in this recipient's same thread.
                # Do not let a dependent failure invalidate the parent's receipt.
                await self._wake_blocked_children()
            return accepted
        except Exception as exc:
            # The claim was committed before invoking transport. Even an error
            # raised during local DTO rehydration cannot prove no network bytes.
            # Preserve the original attempt for read-only reconciliation.
            self.database.platform_delivery_intents.mark_unresolved(
                intent_id,token=claim.token,
                reason=exc.reason if isinstance(exc,PlatformOperationError) else 'unknown_delivery')
            logger.exception('Physical Create unresolved intent_id=%s',intent_id)
            return False
