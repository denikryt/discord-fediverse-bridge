"""Connector-neutral physical Create coordinator over durable SQLite intents.

An adapter is selected by its exact connector. A physical POST may happen only
once for a committed claim; unknown responses stay unresolved until read-only
exact-attempt receipt adoption, never an automatic retry.
"""
from __future__ import annotations

import logging

from ..platform_types import PlatformOperationError
from ..db.repositories.platform_delivery_intents import decode_request
from .platform_delivery_scheduler import DispatchAdmissionClosed

logger=logging.getLogger(__name__)


class PlatformFanoutCoordinator:
    """Serialize recipient Create through durable attempts and typed adapters."""

    def __init__(self, database, registry, *, dispatch_gate=None):
        """Use a Database writer and connector-keyed adapter registry."""
        self.database=database
        self.registry=registry
        self.dispatch_gate = dispatch_gate
        self._delivery_wake = None

    @classmethod
    def from_adapter(cls, database, adapter):
        """Build a strict one-connector registry for the adapter already in runtime.

        The adapter must implement the complete lookup and delivery contract;
        accepting an arbitrary send-only object would bypass registry identity.
        """
        from ..platform_adapters import PlatformAdapterRegistry
        registry=PlatformAdapterRegistry((adapter.connector,))
        registry.register(adapter)
        # A short-lived coordinator inherits the gate explicitly bound to the
        # live adapter by runtime composition; it must not create another gate.
        return cls(database, registry, dispatch_gate=getattr(
            adapter, '_physical_dispatch_gate', None))

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

    def stage_blocked(self, *, domain: str, kind: str, canonical_id: int,
                      forum, parent_canonical_id: int) -> int | None:
        """Reserve a known missing nested parent without freezing or sending a DTO.

        The repository verifies the canonical child/parent and live destination
        owner under the SQLite writer; a future exact parent receipt is the
        only authority for materialization and first physical dispatch.
        """
        placement = self.database.platform_surfaces.resolve_claim(
            forum.connector_key, forum.external_channel_id)
        if (placement is None or placement.kind != 'forum'
            or placement.external_guild_id != forum.external_guild_id
            or placement.connector_key != forum.connector_key):
            return None
        owner_id = {
            'local_host': placement.local_community_id,
            'local_subscriber': placement.local_subscriber_id,
            'remote_subscription': placement.remote_subscription_id,
        }.get(placement.role)
        if owner_id is None:
            return None
        row = self.database.platform_delivery_intents.reserve(
            domain=domain, kind=kind, canonical_id=canonical_id, forum=forum,
            channel_surface_id=placement.id, owner_kind=placement.role,
            owner_id=owner_id, blocked_parent_canonical_id=parent_canonical_id)
        return row.id

    async def recover_pending(self, *, connector_key: str | None = None) -> list[int]:
        """Restart safe Create work without guessing whether old POSTs succeeded.

        Claims from an expired or interrupted transport are made unresolved,
        never pending. Only pristine persisted DTOs with a live canonical
        source and owner are dispatched through the ordinary claim path.
        """
        from .delivery_recovery import recover_pending_creates

        # Recovery and live sends share the same coordinator claims; there is
        # no second queue or alternate SDK transport entry point.
        return await recover_pending_creates(self, connector_key=connector_key)

    def bind_delivery_wake(self, wake) -> None:
        """Inject the one scheduler wake callback during runtime composition."""
        if self._delivery_wake is not None:
            raise RuntimeError('physical delivery wake already configured')
        self._delivery_wake = wake

    async def _wake_blocked_children(self) -> None:
        """Materialize children with exact parent proof, then notify the lanes.

        The parent receipt commits before any child transport call. No nested
        HTTP await is allowed: each connector later obtains its own slot and
        durable claim; a lost notification is recovered by periodic DB scan.
        """
        ready = self.database.platform_delivery_intents.materialize_ready_blocked()
        if ready:
            wake = self._delivery_wake
            if wake is None and self.dispatch_gate is not None:
                wake = self.dispatch_gate.wake
            if wake is not None:
                wake()

    async def _ack_persisted_create(self, *, adapter, claim, attempt, permit,
                                    receipt=None) -> None:
        """Release one completed adapter flight only after a matching SQLite commit.

        The DB remains the authoritative outcome if the optional adapter ACK
        fails; never interpret a cache eviction as physical delivery proof.
        """
        if permit is None:
            return
        proof = self.database.platform_delivery_intents.committed_ack_proof(claim)
        if proof is None:
            return
        terminal = proof.terminal_status
        ack = getattr(adapter, 'acknowledge_persisted_create', None)
        if ack is None:
            return
        try:
            await ack(
                permit=permit, proof=proof, intent_id=claim.intent_id,
                attempt=attempt, request_fingerprint=claim.fingerprint,
                dispatch_token=claim.token, terminal_status=terminal,
                receipt=receipt if terminal == 'delivered' else None,
            )
        except Exception:
            # The committed intent is sufficient for duplicate suppression;
            # failure to evict retains the adapter's conservative capacity.
            logger.exception('Physical Create cache ACK refused intent_id=%s', claim.intent_id)

    async def reconcile_unknown(self, intent_id: int) -> bool:
        """Admit read-only reconciliation through the physical connector slot."""
        if self.dispatch_gate is None:
            return await self._reconcile_admitted(intent_id)
        claim = self.database.platform_delivery_intents.unresolved_claim(intent_id)
        if claim is None:
            return False
        try:
            attempt, _, _, _, _ = decode_request(claim.request_json)
            async with self.dispatch_gate.slot(attempt.connector_key):
                return await self._reconcile_admitted(intent_id)
        except (LookupError, DispatchAdmissionClosed):
            return False

    async def _reconcile_admitted(self, intent_id: int) -> bool:
        """Adopt only a matching read-only adapter receipt; never create again.

        An unknown HTTP outcome (or a new adapter after restart) is not a
        receipt. If the original verified transport future is unavailable,
        this method returns False and leaves the SQLite uncertainty intact.
        """
        claim = self.database.platform_delivery_intents.unresolved_claim(intent_id)
        if claim is None:
            return False
        attempt, _, _, _, _ = decode_request(claim.request_json)
        try:
            adapter = self.registry.get(attempt.connector_key)
        except LookupError:
            # An unavailable adapter cannot perform read-only reconciliation.
            # Keep the durable unknown-effect fence untouched for a later run.
            return False
        inspector = getattr(adapter, '_reconcile_persisted_create', None)
        if inspector is None:
            return False
        evidence = await inspector(
            intent_id=claim.intent_id, attempt=attempt,
            request_fingerprint=claim.fingerprint, dispatch_token=claim.token,
        )
        if evidence is None:
            return False
        receipt, proof, permit = evidence
        if not self.database.platform_delivery_intents.commit_receipt(
            intent_id, token=claim.token, receipt=receipt,
            reconciliation_proof=proof,
        ):
            return False
        await self._ack_persisted_create(adapter=adapter, claim=claim, attempt=attempt,
                                         permit=permit, receipt=receipt)
        await self._wake_blocked_children()
        return True

    async def deliver(self, intent_id: int) -> bool:
        """Take the connector slot before any durable Create claim or remote I/O."""
        if self.dispatch_gate is None:
            return await self._deliver_claimed(intent_id)
        pristine = self.database.platform_delivery_intents.pristine_pending_request(intent_id)
        if pristine is None:
            return False
        try:
            attempt, _, _, _, _ = decode_request(pristine[0])
            async with self.dispatch_gate.slot(attempt.connector_key):
                # Re-evaluate the intent inside admission; another sender may
                # have settled it while this task waited for capacity.
                return await self._deliver_claimed(intent_id)
        except (LookupError, DispatchAdmissionClosed):
            return False

    async def _deliver_claimed(self, intent_id: int) -> bool:
        """Send only a newly claimed immutable Create, commit verified receipt.

        Return False for ineligible/already handled/unknown attempts. The
        durable intent rather than this return value decides restart behavior.
        """
        repository = self.database.platform_delivery_intents
        pristine = repository.pristine_pending_request(intent_id)
        if pristine is None:
            return False
        # Unavailable adapters are never allowed to turn pristine work into
        # an unknown remote write. Resolve identity before the SQLite claim.
        request_json, fingerprint = pristine
        try:
            attempt, _, _, _, _ = decode_request(request_json)
            adapter = self.registry.get(attempt.connector_key)
            readiness = getattr(adapter, 'dispatch_ready', None)
            if readiness is not None and not readiness():
                return False
        except (LookupError, ValueError):
            return False
        claim = repository.claim(
            intent_id, expected_request=request_json, expected_fingerprint=fingerprint,
        )
        if claim is None:
            return False
        io_started = False
        try:
            attempt,content,presentation,title,reply_to=decode_request(claim.request_json)
            # A valid claim may become ineligible during registry selection.
            # Re-read canonical deletion and the current surface owner at the
            # last authoritative pre-transport boundary, never from cache.
            if not self.database.platform_delivery_intents.dispatch_still_authorized(claim):
                repository.release_unstarted_claim(claim)
                return False
            # Permit issuance is adapter-local and only occurs after a fresh
            # committed SQLite dispatch claim. Non-supporting adapters cannot
            # evict unknown cache flights and keep their own fail-closed policy.
            permit = None
            kwargs = {}
            issuer = getattr(adapter, '_mint_coordinator_dispatch_permit', None)
            if issuer is not None:
                proof = self.database.platform_delivery_intents.committed_dispatch_proof(claim)
                if proof is None:
                    # No proof of live dispatch => do not call the physical API.
                    raise PlatformOperationError('invalid_request', 'durable dispatch claim invalid')
                permit = issuer(
                    proof=proof, intent_id=claim.intent_id, attempt=attempt,
                    request_fingerprint=claim.fingerprint,
                    dispatch_token=claim.token, operation=attempt.operation,
                    destination=attempt.destination, content=content,
                    presentation=presentation, title=title, reply_to=reply_to,
                )
                kwargs['dispatch_permit'] = permit
            # No fallback to a Discord singleton or raw SDK path. Distinguish
            # the three operation types before constructing a network request.
            # Recheck readiness after the claim, but before method entry.
            # Only this witnessed interval authorizes release to pending.
            readiness = getattr(adapter, 'dispatch_ready', None)
            if readiness is not None and not readiness():
                repository.release_unstarted_claim(claim)
                return False
            # After entering even one adapter method, Python cannot prove the
            # Node/SDK side did not commit a write before cancellation.
            io_started = True
            if attempt.operation=='forum_thread':
                receipt=await adapter.create_forum_thread(
                    attempt.destination,content,title=title,presentation=presentation,
                    attempt=attempt,**kwargs)
            elif attempt.operation=='reply_thread':
                receipt=await adapter.create_reply_thread(attempt.destination,title=title,
                                                          attempt=attempt,**kwargs)
            elif attempt.operation=='message':
                receipt=await adapter.send_message(
                    attempt.destination,content,reply_to=reply_to,
                    presentation=presentation,attempt=attempt,**kwargs)
            else:
                raise PlatformOperationError('invalid_request','unsupported physical create operation')
            # A successful return still requires exact typed receipt validation
            # at the repository boundary, inside the mapping transaction.
            accepted = self.database.platform_delivery_intents.commit_receipt(
                intent_id,token=claim.token,receipt=receipt)
            if accepted:
                await self._ack_persisted_create(
                    adapter=adapter, claim=claim, attempt=attempt,
                    permit=permit, receipt=receipt,
                )
                # A confirmed physical comment becomes the exact prerequisite
                # for any children waiting in this recipient's same thread.
                # Do not let a dependent failure invalidate the parent's receipt.
                await self._wake_blocked_children()
            return accepted
        except BaseException as exc:
            # Only this coordinator knows whether it crossed the adapter method
            # boundary. A failed local preflight can safely release its token;
            # any entered I/O (including cancellation) remains uncertain.
            if not io_started:
                repository.release_unstarted_claim(claim)
            else:
                persisted = repository.mark_unresolved(
                    intent_id, token=claim.token,
                    reason=exc.reason if isinstance(exc, PlatformOperationError) else 'unknown_delivery',
                )
                if persisted and 'adapter' in locals() and 'permit' in locals():
                    await self._ack_persisted_create(
                        adapter=adapter, claim=claim, attempt=attempt, permit=permit,
                    )
            if not isinstance(exc, Exception):
                raise
            logger.exception('Physical Create refused/uncertain intent_id=%s', intent_id)
            return False
