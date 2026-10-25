# Stage 04 S-01 — выполненная часть и оставшийся объём

**Verdict: PARTIALLY IMPLEMENTED; S-01 NOT COMPLETE.** No release readiness claim.

## Input and audit precedence

- Applied the user-provided 12-patch `stage04_checkpoint_patches_056_063_plus_four_review_fixes.tar.gz` to clean `53e9311958959c925c7522fe629d66690ae9ce1f` without commits. Baseline tree **`d8bf4f457b9f81328e4f67530776be46090870f3`** matches manifest.
- **Before** editing production code, wrote `/mnt/data/stage04_s01_legacy_create_audit.md` and the repository copy `docs/development/stage04_s01_legacy_create_audit.md`. The audit verified the production normalized callback and found **62 direct raw Create invocations in 14 test files**, rather than only three modules as suggested in the review. No explicit production call sites for `DiscordEventRouter.handle_thread_create()` / `.handle_message()`, but external/dynamic consumers cannot be excluded statically.

## Applied changes

- Deleted obsolete `DiscordEventRouter.handle_thread_create` and `.handle_message` SDK-shaped Community Create entry points. Kept the actual normalized handlers, Personal Feed owner, and existing physical Update/Delete handlers.
- Migrated all **five raw-router call sites** across three focused Local Community suites into verified `ThreadCreated`, `MessageView`, and `MessageCreated` inputs. Replaced their mocks with normalized runtime methods; a previously expected fallback from an inactive/unclaimed forum into the Remote runtime is now a fail-closed no-op, consistent with typed ingress authorization.
- Added `tests/support/typed_forum_creates.py` and `tests/test_stage04_single_community_create_route_contract.py` to check the production router cannot regain raw dispatch.
- Documented scope and outstanding consumers in event-flow docs, test coverage map and `notes/known_issues.md`.

## Checks

- Baseline manifest tree `d8bf4f...`: PASS.
- Red/green TDD: two AST source-contract tests failed before router removal and passed after.
- Test-focused real SQLite scenarios with only import-time `hypothesis` settings and `discord` modules stubbed (no real SDK calls): **28 passed** from normalized Remote/Local runtime and source persistence modules, **4 passed** migrated router tests, 17 unrelated old tests deselected.
- Independent full run of the three legacy behavior modules with the same import stubs: **6 passed, 15 failed**. The failing cases directly exercise the *old* raw runtime APIs with missing published timestamps, old send-before-commit gateway mocks, unsupported missing durable author mutation service, or require full discord.py module. This is a **real remaining migration blocker**, not acceptance or proof of a new production regression. Do not silently skip or delete them.
- `python -m compileall -q src tests`: PASS. `git diff --check`: PASS. No full pytest (Hypothesis and discord.py unavailable), no Node/Fedify/live Discord run.
- S-01 patch applied atop exact 12-patch baseline in independent Git index: PASS; complete cumulative applied to clean base: PASS. Both produce identical Git tree **recorded in the external S-01 verification manifest**. No Git commits, push or deployment.

## Still required for full S-01 closure

- **57 direct calls in 14 test files** remain against `CommunityRuntime.handle_discord_thread_create/handle_discord_message` or `LocalCommunityRuntime.handle_discord_thread_create/handle_discord_message`. Convert every distinct assertion to modern typed source/sibling/failure/parent/dedup scenarios using render-only gateway and authenticated timestamp, not a test compatibility shim or deletion of test coverage. The remaining direct callers still bind old runtimes.
- Once all direct test callers are gone and positive/negative behavior has been preserved, remove `CommunityRuntime` and `LocalCommunityRuntime` raw Create methods and dead `LocalCommunityDiscordCreateHandler` host/subscriber helpers. Inspect method-level `DiscordFanout`/`LocalCommunityDiscordFanout` callers before deleting SDK helpers: some are still used by other paths or tests. Audit external reflective callers explicitly as unsupported/breaking API; don't silently install fallbacks.
- Re-run full Python/Node matrix with actual dependencies, including Personal Feed and inbound ActivityPub paths. Only then state that no second raw Community Create implementation remains.

**No guarantee of complete S-01 simplification at this stage.** The achieved simplification is removal of the unused raw entry from the only production Discord router, with test migration and a negative regression contract. Raw runtime implementation is intentionally retained until its remaining test assertions can be migrated without loss.
