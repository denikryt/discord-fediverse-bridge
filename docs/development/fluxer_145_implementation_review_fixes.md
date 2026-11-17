# Fluxer-only #12 — implementation review fixes (F1–F5)

**Source:** `fluxer_145_implementation_review.md` against `34e1c804`.
**Fix branch:** `feature/fluxer-only-runtime-145-review-fixes`.
**Scope:** only the five reproduced review defects; canonical Fluxer federation remains #13/#14.

| Finding | Change | Regression evidence |
| --- | --- | --- |
| F1 — mixed SQLite provenance | Read-only pre-migration classifier checks every v1 connector FK, Discord `source_platform`, Discord integer projections and old invite ownership, even when the source surface is NULL. | Surface-less Discord mapping, conflicting platform/FK, authored object, physical copy, actor key preservation and genuine Fluxer copies. |
| F2 — stale/unproven write capabilities | Production capability flags combine fresh SDK metadata, current bot identity, action ACL and verified provider contract; direct effectful RPC rejects unverified builds. | Allowed→revoke→grant, unknown roles, thread bit 38, closed thread, unselectable required tags, unverified server contract and zero denied SDK writes. |
| F3 — Discord scenario fixtures | Scenario builders and direct HTTP/AP fixtures explicitly select Discord; user Follow mode read is deferred until event kind and target are known. | Dashboard, registration, AP inbound and user Follow scenarios. No production fallback added. |
| F4 — empty platform selector | `Settings()` preserves an explicitly empty `BRIDGE_PLATFORM` from real env or `.env`; Compose uses `${BRIDGE_PLATFORM-discord}` to distinguish unset from empty. | Unset/empty/whitespace/invalid env, `.env` and precedence tests; Compose static mode tests. |
| F5 — Compose health origin | Node probe compares READY origin with `new URL(FLUXER_ORIGIN).origin`. | Real Node-executed probe accepts equivalent canonical URL spellings and rejects foreign origin, wrong key, absent bot, degraded state and Gateway gap. |

## Local verification (this environment)

- `python -m unittest tests.behavior.test_fluxer_single_trusted_provisioning tests.test_fluxer_connector_config tests.behavior.test_fluxer_compose_topology -q`: **30 pass**.
- `PYTHONPATH=tests python -m pytest --noconftest -q tests/behavior/test_fluxer_single_trusted_provisioning.py tests/behavior/test_fluxer_compose_topology.py tests/behavior/test_fluxer_runtime_composition.py tests/behavior/test_fluxer_gateway_ingress.py tests/behavior/test_single_platform_runtime.py tests/test_fluxer_connector_config.py`: **55 pass, 1 skip, 51 subtests pass**.
- `node --experimental-strip-types --test fluxer-connector/tests/production_dispatch.test.mjs fluxer-connector/tests/permissions.test.mjs fluxer-connector/tests/service.test.mjs fluxer-connector/tests/capabilities.test.mjs`: **29 pass**.
- All Node test files: **118 pass**, two cannot load without `@fluxerjs/core`.
- Four selected legacy Python suites with `--noconftest`: **41 pass**, one existing SDK-dependent inbound fanout scenario fails because `discord.py` is unavailable (no `bridge_platform` `AttributeError` in those executed tests).
- `npm --prefix fluxer-connector run check`: **blocked**, local `@types/node` is absent.
- Normal full `pytest` collection: **blocked**, `hypothesis` unavailable; full Python F3 regression count therefore remains unconfirmed.
- Docker Compose engine, real Fluxer SDK/REST/Gateway and runtime secret/namespace verification: **not run**.

## External acceptance required

1. Install dependency-locked Python, Fluxer Node and Fedify Node packages; run full Python/Node/Fedify suites and TypeScript builds. Confirm the review's 71 additional `AttributeError` failures are gone; compare genuine baseline tag where required.
2. On a recorded isolated Fluxer server build, leave `FLUXER_VERIFIED_WRITE_CONTRACT=false` until the server write contract can be positively tested. Set it to `true` **only in an isolated acceptance deployment**, exercise forum tag rules, thread replies, bot-owned edit/delete, permission revoke/regrant, SDK readbacks and gateway reconnect. Do not treat the flag or fake SDK tests as proof of live compatibility.
3. Run actual Discord-only and Fluxer-only Compose projects with distinct domains/SQLite volumes/ports, confirm effective interpolated config, Node-only bot secret, shared loopback namespace, health origin normalization, sidecar restart and graceful full shutdown.
4. Do not close issue #12 until S19 live tests pass. None of these changes activates #13/#14 community federation or account linking.
