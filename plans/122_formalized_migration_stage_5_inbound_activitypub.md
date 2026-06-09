# 122 — Formalized Migration Stage 5: Inbound ActivityPub and Dedup

## Scope
Classify inbound ActivityPub outcomes, policy/ban enforcement, backfill, dedup, mappings, and local-community routing without replacing protocol-rich scenarios.

## Classification
- Typed/technical outcome cases: existing receipt outcome and schema tests.
- Named scenarios with metadata: accepted/skipped/deferred/failed/duplicate flows, local-community Follow/Undo/content, unsubscribed skips, backfill, and shared-group routing.
- Technical contracts: authenticated internal Fedify read API.
- Duplicate removal: none.

## Implementation
Add a domain-specific manifest and passive report over stable pytest node prefixes. Preserve payload-specific assertions and historical phase narratives. Explicitly represent outcome, enforcement, dedup, backfill, mapping, and technical API families.

## Boundaries
No outbound content lifecycle migration, universal payload generator, production dispatch-table oracle, or historical-test deletion.

## Verification
Focused inbound/report tests, full Python/gateway gates, zero missing declared rules, commit and bundle.
