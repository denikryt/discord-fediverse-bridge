# Single Community Create pipeline

This document records the Community Create cutover and its verification boundary.

## Implemented

- Removed the raw router and all four raw Local/Remote runtime Create methods.
- Removed the four local host/subscriber raw Create handlers and unused SDK
  Create fanout methods. Typed fanout uses `PlatformFanoutCoordinator`; physical
  Update/Delete and Personal Feed retain their existing owners.
- Migrated the final 32 direct raw Create calls across eight scenario files.
  Source observations include author, forum, guild, container and original time.
  The tests exercise real SQLite admission, durable intents and per-copy receipts.
- Phase 5 inbound fixtures now supply the bound platform adapter. Five existing
  inbound scenarios retain post/comment, dedup and per-target parent assertions.
- Retry scenarios distinguish committed canonical state before first dispatch
  from uncertain POST outcomes. No test authorizes another POST after an unknown
  result merely because the source event was repeated.
- Preserved registered-handle attribution on Remote Community mirror creates.
- MIG-01: incomplete legacy mapping identity is checked under the reserved SQLite
  writer before insertion, including conflicts with typed container evidence.
  Exact receipt retries are idempotent; distinct fully scoped containers remain
  independent. MIG-02: the released-schema migration expects 47 current tables.
- Behavioral harness imports no longer rely on a `tests` namespace that conflicts
  with the vendored discordops test package.

## Compatibility and verification

No repository consumer invokes a removed Create method. External reflective
consumers cannot be proved absent; these raw methods are intentionally removed,
without aliases or SDK fallback. Callers must submit verified typed events.

The delivered verification report contains exact dependency-backed suite results,
comparison with the input tree, and remaining failures or environment limits.
Completing this cutover does not by itself establish live Discord/Fedify acceptance.
