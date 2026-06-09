# 133 — Local-Community Relay Stage 6 Boundary Failures

## Problem / Goal

Record and test the actual durability boundaries of local-community relay when rendering, gateway transport, or per-outcome persistence fails.

## Current Contracts

- Policy snapshot failure occurs before source/delivery persistence.
- Renderer failure occurs after source and pending delivery rows are committed, before transport.
- A raised gateway call leaves committed pending rows with zero attempts.
- Result persistence is per outcome in separate repository sessions; a later persistence failure can leave earlier outcomes committed and later rows pending.

## Scope

Add deterministic and generated boundary-failure tests over the real fanout and SQLite repository. Do not change production transaction behavior in this stage. Record partial result persistence as a known operational limitation.

## Touched Files

- tests/support/local_community_relay.py
- docs/development/test-assurance.md
- notes/known_issues.md

## New Files

- tests/model_based/test_local_community_relay_boundary_failures.py

## Tests

Verify source rows, delivery statuses, attempt counts, transport calls, and retryability after each boundary failure. Run all model, behavior, Python, compile/diff, and gateway checks.
