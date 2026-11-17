# Fluxer-only #12 — re-review F1–F4 implementation and verification

This note records changes applied after the `fluxer_145_fixes_rereview.md` review
of the `3faacdef` branch. It does **not** establish S19 live acceptance.

| Finding | Corrected boundary | Evidence |
| --- | --- | --- |
| F1 / S04 | `preflight_instance_mode()` rejects unscoped Discord `source_*_id` projections in both canonical group tables, despite NULL connector/surface IDs. It continues to admit scoped Fluxer groups with NULL legacy projections. | SQLite FK-valid negative cases through real `build_runtime()` and byte-identical DB before/after refusal; positive Fluxer group control. |
| F2 / S12 | Production `thread_replies` requires both fresh `send_message=allowed` and `read_history=allowed`, as does the physical thread executor. | Authenticated production dispatcher, independently revoked/regranted `ReadMessageHistory`, forbidden SDK write with zero effects and successful write after recovery. |
| F3 / S18 | Remaining Discord fixtures now carry explicit `bridge_platform='discord'`; no production fallback for missing settings was added. | 40 selected Python tests / 3 subtests pass with `--noconftest`; selected legacy actor-read tests and direct Follow test also pass. The exact historical 71-case full re-run remains unavailable here. |
| F4 / S01 | Empty selector is preserved at the ordinary Pydantic source priorities; absent selector defaults to Discord. Other optional empty env fields remain ignored. | Case-insensitive env, env-over-dotenv, constructor-over-env and ordered multi-file `.env` tests pass. |
| F5 / S17 | No further change; canonical-origin Compose healthcheck from previous fix retained. | Existing Compose static scenarios pass. |

## Available test runs

- `python -m unittest tests.behavior.test_fluxer_single_trusted_provisioning tests.behavior.test_single_platform_runtime.ExclusiveConfigurationContracts tests.behavior.test_single_platform_runtime.SettingsPlatformSourceContracts -q`: PASS.
- `node --experimental-strip-types --test fluxer-connector/tests/production_dispatch.test.mjs fluxer-connector/tests/permissions.test.mjs fluxer-connector/tests/message_send.test.mjs`: 22 PASS.
- `python -m pytest -q --noconftest tests/behavior/test_fluxer_compose_topology.py tests/behavior/test_fluxer_single_trusted_provisioning.py tests/behavior/test_profile_media_scenarios.py tests/behavior/test_activitypub_global_ban_routing.py tests/test_internal_fedify_api.py`: 40 PASS, 3 subtests PASS.
- Targeted legacy actor-read route and User Actor Follow tests: PASS.
- `node --experimental-strip-types --test fluxer-connector/tests/*.test.mjs`: 119 PASS, 2 SDK-import test files FAILED TO LOAD (`@fluxerjs/core` missing), not functional assertions.
- `python -m pytest -q`: blocked loading `tests/conftest.py`, `hypothesis` missing. Several Discord scenarios cannot import `discord.py`; package installation unavailable through this environment's index.
- TypeScript compilation and actual Docker/Fluxer server tests were **not run**; Docker CLI and required SDK dependencies/credentials are unavailable.

## Release boundary

`FLUXER_VERIFIED_WRITE_CONTRACT` remains an opt-in operator setting, disabled by
default. It is not evidence that a particular server build was tested. S19 must
be executed in an external environment with lockfile dependencies, real Fluxer
REST/Gateway writes/readbacks, authenticated reconnect, denied writes, actual
Discord-only/Fluxer-only Compose deployments and secret/namespace inspections.

## RR06 — one-pass dotenv source (2026-11-17)

The independent RR01–RR05 review found that the `empty_dotenv()` overlay
opened `.env` a second time after Pydantic had already consumed it. A FIFO
carrying a single valid or empty `BRIDGE_PLATFORM` caused startup to block.

`Settings` now substitutes one selective `DotEnvSettingsSource` at the normal
dotenv priority while disabling the eager default source's file read. Each
file is parsed exactly once. Per-file filtering retains an explicit empty
`BRIDGE_PLATFORM` for validation while ignoring empty non-selector values
before multi-file merge. Process environment case normalization and the
`init > env > dotenv > file secrets` precedence remain Pydantic-owned.
The implementation is validated against the locked `pydantic-settings 2.14.1`.

`python -m pytest -q --noconftest tests/behavior/test_single_platform_runtime.py
 tests/behavior/test_fluxer_single_trusted_provisioning.py
 tests/behavior/test_fluxer_runtime_composition.py
 tests/behavior/test_fluxer_compose_topology.py tests/test_fluxer_connector_config.py`
completed with **54 passed, 1 skipped and 45 subtests passed**. The new FIFO
subprocess regression tests supply credentials once, check both success and
an immediate validation failure, and terminate the child on timeout.

This corrects the local RR04 behavior; it does not establish S19 live
REST/Gateway or actual Docker acceptance. No Node or database schema changed.
