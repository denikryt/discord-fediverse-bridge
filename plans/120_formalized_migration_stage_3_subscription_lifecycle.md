# 120 — Formalized Migration Stage 3: Subscription Lifecycle

## Scope
Formalize deterministic subscribe/unsubscribe lifecycle decisions while preserving protocol exchange, Accept(Follow), Undo, retry, and gateway narratives.

## Classification
- Typed cases: operation-level registration, existing subscription/follow state, last-channel cleanup, missing follow ID, and remote cleanup outcome.
- Named scenarios: multi-step subscription, Accept(Follow), retry, duplicate ActivityPub, and gateway verification remain explicit narratives.
- Technical integration: gateway TypeScript Follow/Accept/Undo verification remains native.
- Duplicate removal: none.

## Implementation
Add domain-specific lifecycle cases/effects, execute real DiscordOps operations against explicit repository/gateway fakes, and emit a passive report. The report also records retained narrative/gateway rule ownership in documentation.

## Boundaries
No message fanout, generic retry framework, concurrency, or protocol-test replacement.

## Verification
Focused operation contracts and existing subscription/follow/unfollow/retry tests; full Python/gateway gates; deterministic zero-gap report.
