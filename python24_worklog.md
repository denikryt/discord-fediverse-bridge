# PR #20 — Python baseline failures remediation worklog

## 2026-10-21 — setup
- Restored isolated checkout at `05ef104` and applied the seven-patch cumulative diff. Production data and Docker resources untouched.
- Source: user-supplied pytest log (24 failed, 1729 passed) and seven-patch bundle.
- Local Python 3.13.5 lacks `discord.py` and `hypothesis`. Package installation attempted via pip; registry DNS resolution failed. No claim of full pytest execution here.
- Inspect failing scenarios against current Platform API and preserve observable behavior. Changes will be exported as an eighth incremental patch; validate with static checks and isolated possible reproductions.

## Source and scenario audit
- Inspected all 24 failed test IDs from `verification_accept_pytest.log`, current DiscordAdapter/BridgeBot event routing, PersonalFeedDiscordDeliveryWorker, Local Community fanout, immutable MessageMappingRepository, RelayAttempt repository, and deploy secret boundary.
- Existing-model relay tests expected exceptions and unused attempt counters; production now isolates renderer and gateway failures per target and records failed generation 1. Rewrote assertions to check actual independently retryable delivery rows.
- Updated three personal-feed worker fixtures and one shutdown fixture from retired `bot=` transport to typed adapter. Reused `RecordingAdapter` from the existing adapter-worker tests.
- Updated BridgeBot Gateway callback fakes to model synchronous enter/leave admission and normalize content into DTOs. Added isolated `tests/support/typed_mutation_ingress.py` for actual bot -> normalized handler -> DB cases without a Discord network call.
- Fixed Local Community post fixtures to retain source Create proof. Duplicate relay replay test now explicitly supplies previously committed authenticated source proof; the production handler correctly refuses recovery from an unverified historical mapping.
- Found a real generic AP mapping conflict: a valid Local Community comment created a distinct physical copy but tried to rewrite an earlier immutable generic mapping. A narrow same-identity check now preserves the generic receipt, without accepting changed AP IDs/actors.
- Found a real fail-closed boundary: `LocalCommunityDiscordFanout._surface_is_allowed()` did not catch ownership repository errors. Scoped the existing exception protection to cover the ownership check too.
- Corrected old URL fixtures rather than weakening `test_no_old_local_activitypub_url_construction`. Updated Compose secret boundary test: only bridge, not gateway, may inherit the root env_file.

## Verification available in this environment
- `python -m compileall -q src tests`: PASS (syntax for all current Python files).
- Direct `test_no_old_local_activitypub_url_construction()`: PASS on all tracked source and fixture files.
- Direct `test_deployment_uses_one_root_env_file()`: PASS.
- Extracted actual `_surface_is_allowed` method and executed it against malformed guild and failing repository: both FAIL CLOSED.
- `TypedMutationIngress` exercised with real `MessageUpdated`/`MessageDeleted` dataclasses: PASS, IDs/body/timestamp preserved.
- `git diff --check`: PASS.
- `discord.py` and `hypothesis` unavailable; `pip install` failed because package registry DNS resolution failed. Consequently the full pytest suite cannot be rerun here and **no full-suite PASS is claimed**.
- All modifications are confined to an isolated Git checkout. Production Docker/credentials/network volumes were not accessed.

## Remaining verification requirements
- Apply the eighth patch after the seven verified patches in the user's separate test worktree; use the pinned project Python dependencies and execute full pytest.
- Diagnose any remaining changed-ID failures, and explicitly verify new generic mapping handling under concurrent writes, typed raw-event dispatch, and relay partial failures.
- Do not push, merge, or deploy until the complete suite and release security gates are reviewed.
- Executed the actual `_persist_inbound_activitypub_message_mapping` method isolated from SDK imports: PASS for existing same-identity receipt preservation, incompatible actor rejection, and first insert.

## Final patch review
- Removed dead SDK-era helpers/fakes from migrated personal-feed and raw mutation tests; the current neutral event contracts are exercised instead.
- Final egress is an incremental eighth patch against the seven-patch baseline, plus a cumulative patch against `05ef104`. Both need verification with real installed dependencies; no release claim is made.
- Prepared a standalone follow-up report at `/mnt/data/discord_adapter_python_baseline_report.md` with release limitations, prior baseline counts and isolated rerun instructions.

## Artifact reproducibility
- Reconstructed a new isolated checkout from `05ef104`, then applied cumulative patches 1–7 and incremental patch 8 in order. `git diff --check` and `compileall src tests` passed. The Compose and retired-URL contract tests executed directly and passed. The new typed helper and changed source files matched the original working tree byte-for-byte.
