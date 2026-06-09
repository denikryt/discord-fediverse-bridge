# 121 — Formalized Migration Stage 4: Outbound Fanout and Routing

## Scope
Formalize outbound routing by bounded rule families while preserving multi-target narratives and Stage 9 failure-ordering scenarios.

## Classification
- Typed/repeated decision coverage: existing parameterized routing-metadata tests remain executable decision cases.
- Named scenarios with metadata: remote subscription fanout, local subscriber fanout, origin-surface behavior, retry, receipts/mappings, and failure ordering.
- Generated assurance: existing deterministic failure/concurrency tests remain classified as generated/fault coverage.
- Duplicate removal: none.

## Implementation
1. Add a domain-specific manifest of stable rule IDs, families, classifications, and pytest node prefixes.
2. Add a passive report runner that executes the selected fanout files and records status for every represented node.
3. Validate target selection, malformed metadata, fail-closed policy, healthy-target isolation, mapping/receipt retry, local/remote modes, and origin/sibling behavior.
4. Preserve full target-specific assertions in the original tests; the report does not normalize them away.
5. Defer shared delivery/mapping/receipt fragments: subscription contracts use operation-call effects, while fanout narratives assert richer per-target state.

## Boundaries
No inbound parsing, edit/delete lifecycle migration, universal fanout case, or opaque aggregate effects.

## Verification
Manifest/report tests, all selected fanout scenarios, full Python/gateway gates, deterministic zero-gap report, commit and bundle.
