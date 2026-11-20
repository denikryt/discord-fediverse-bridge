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

## RR07 — Pydantic Settings deployment version contract (2026-11-17)

The next review confirmed that the RR06 one-pass `.env` source relies on
`BaseSettings._settings_init_sources()`, which is not present in the previously
advertised minimum `pydantic-settings 2.8.0`. At that version a later empty
non-platform value in a sequence of `.env` files can overwrite a preceding
nonempty value; a container built by `pip install .` could therefore behave
differently from the uv-locked development environment.

The Python project now requires **exactly `pydantic-settings==2.14.1`** in
`pyproject.toml`, matching the already locked version in `uv.lock`. The Docker
runtime resolves this exact requirement through `pip install .`; it does not
use `uv.lock` as an installation source. This intentionally pins the private
source-hook ABI until a public, single-pass alternative is implemented.
Changing the pin requires repeating the settings source-order, case folding,
FIFO, and multi-file-empty regression tests as well as the image build.

The new dependency-contract test checks the Python package requirement,
locked version and Docker install path. Existing positive/negative runtime
settings tests continue to exercise selector validation and a one-shot FIFO.
This change does **not** provide live Fluxer or Docker S19 acceptance.

## Full re-verification D1–D4 (2026-11-17)

The independent full re-verification of `9943786` found four concrete
contract gaps; the fixes below are made on a dedicated successor branch.

- **D1 / S10:** ThreadRef writes now preflight the verified *thread* ID for
  `thread_replies`, `view_channel`, `read_history` and `send_message`, and check
  parent visibility separately. They do not require `SendMessages` in the
  parent forum. The Node SDK executor still independently enforces bit 38 and
  checks the exact immediate reply parent. A cross-process matrix revokes
  forum `SendMessages` and thread bit 38 independently; allowed nested replies
  and forbidden writes have observable SDK-boundary counts.
- **D2 / S03–S04:** The reserved `discord-primary` key is refused by Python
  connector settings, read-only pre-migration admission and Node startup.
  Both an existing empty SQLite file and an inert current-v1 SQLite database
  remain byte-identical after direct preflight refusal.
- **D3 / S03–S07:** Final-dot DNS origins are refused at Python connector
  admission and Node `validateOrigin()` before SDK login. Canonical mixed-case
  and default HTTPS port spellings still normalize to the same instance;
  mismatched origins cannot attest SDK readiness.
- **D4 / S03–S06:** Python settings reject a private IPC bearer shorter than
  eight characters, matching Node startup. Neither the credential nor the
  rejected raw JSON is included in error text.

Tests use actual Python adapter / authenticated Node RPC / SQLite entry points
and an SDK-boundary fake, not a live Fluxer server. The local regression run
completed **87 Python tests** and **34 targeted Node tests** (recounted in
release log). The full available Node suite loaded 121 tests and reported two
files unable to load because `@fluxerjs/core` is absent. Full `pytest` cannot
load its `hypothesis` dependency, and TypeScript typecheck cannot find Node
types in this environment. These are **not** proof of S19 acceptance. Actual
Fluxer REST/Gateway and Docker deployments still require external verification.
