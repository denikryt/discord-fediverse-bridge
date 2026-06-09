# 123 — Formalized Migration Stage 6: Content Lifecycle

## Scope
Formalize publish, reply, edit, and delete contracts across Discord, ActivityPub, local-community, and mirrored directions while preserving conversation narratives.

## Classification
- Named scenarios with metadata: cross-direction publish/reply/edit/delete flows, message trees, mirror propagation, and failure isolation.
- Typed/technical cases: existing focused publish-flow and dispatch-routing tests.
- Generated assurance: existing dedup/out-of-order tests.
- Duplicate removal: none.

## Implementation
Add a domain-specific manifest and passive report covering creation, parent resolution, dedup, mapping, edits, deletes, local/remote origins, mirrored directions, and partial failure. Preserve rich payload and message-tree assertions.

## Framework checkpoint
Delivery/mapping/mutation fragments remain deferred until Stage 8: fanout, inbound, and content domains share concepts but not yet one safe observed-effect shape.

## Boundaries
No dashboard/config migration, performance work, public behavior changes, or normalized summaries replacing payload assertions.

## Verification
Focused lifecycle/report tests, full Python/gateway gates, zero missing rules, commit and bundle.
