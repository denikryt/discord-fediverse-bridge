# 119 — Formalized Migration Stage 2: Identity, Discovery, and Registration

## Scope
Formalize deterministic identity normalization, community resolution, and label presentation contracts. Preserve registration and directory-refresh end-to-end narratives.

## Classification
- Typed cases: remote actor-handle normalization/extraction, community selection resolution, and relay label formatting.
- Named scenarios: registration, unified discovery, and directory snapshots remain narrative with their current assertions.
- Technical integration: database identity tests remain unchanged.
- Duplicate removal: none in this stage.

## Implementation
1. Add domain-specific identity/discovery cases with stable IDs and independent expected values/errors.
2. Execute real pure helpers and async resolver paths with narrow network-edge fakes.
3. Add a deterministic passive report covering declared normalization, resolution, ambiguity, and label rules.
4. Keep registration ownership/idempotency and directory refresh scenarios intact.
5. Re-evaluate shared framework extraction and defer it because community-management and identity inputs/effects differ materially; only passive collector remains shared.

## Boundaries
No ActivityPub delivery, fanout, universal identity hierarchy, or replacement of database integration tests.

## Verification
Focused cases/report, retained identity/discovery/registration scenarios, full Python/gateway suites, compile/diff checks, commit and bundle.
