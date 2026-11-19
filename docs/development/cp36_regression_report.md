# Plan 146 CP46 — local regression report

Candidate: `feature/federated-user-feeds`, CP45 `2779aa9` plus this CP46 documentation and fixture contract update. Baseline: CP35 `fa950136`. Tests run in the isolated container using Python 3.13.5 and Node 22.16.0; **no live Discord, NodeBB or signed peer traffic was available**. All SQLite tests use disposable databases.

## Repeatable commands and results

| Run | Observation | Interpretation |
| --- | --- | --- |
| `python -m pytest -q` | `BLOCKED` while loading `tests/conftest.py`: `ModuleNotFoundError: hypothesis` | Application suite not run; **not** a green gate. |
| `python -m pytest --noconftest -q tests/behavior` | `BLOCKED` at 44 collection errors; missing `discord.py`, vendor `tests` namespace, and other unrelated imports | Global behavioral suite not comparable with the CP35 field suite. |
| CP36–CP45, Plan 145 rendering/follow, Remote Person delivery/commands/channel scenarios | **84 passed, 679 passed subtests** with `--noconftest` and process-only `tests` namespace bootstrap | Product code and SQLite/transport boundaries, not live peer delivery. |
| `test_remote_person_follow_lifecycle.py` separately | **17 passed, 1 skipped**; SDK-only handler test skips without `discord.py` | Same test yields an import failure when another test installs a partial fake `discord` module in a shared pytest process. No production-code failure inferred. |
| `python -m compileall -q src tests/behavior` | PASS | Python syntax only. |
| `cd fedify-gateway && npm run check` | `BLOCKED`: missing `@fedify/*`, `@js-temporal/polyfill`, Node type packages; numerous TS2307 and derived type errors | The 59 Gateway scripts cannot be executed in this container. Modified TS sources additionally passed the globally installed TypeScript parser. |
| `git diff --check` | PASS | Whitespace / patch integrity only. |

The original Plan 145 assertion that a fitting link must be **omitted** was updated only because R09 explicitly supersedes it: a whole fitting link crossing the prefix cutoff must now survive a middle omission. The new regression also asserts this directly for complete raw, angle and Markdown links. No baseline unrelated assertion was modified.

## CP35 comparison limits

The supplied CP35 live report recorded **1217 passed, 215 failed, 6 errors, 1 xfailed** for Python, DiscordOps **50 passed**, and Gateway **53 passed, 6 failed**. Those are field-review measurements with the locked CP35 environment; this container cannot reconstruct its test ID/status/reason matrix from counts. CP36's baseline records the same dependency failures as today. Consequently **no per-ID matched-environment regression comparison can be asserted**. Baseline failures must not be relabelled as fixed or new regressions without that evidence.

The six Gateway CP35 failure groups were: `community-follow-sender`, `direct-update-delete-delivery`, `normalize-reply-chain`, `published-object-store`, `python-bridge-read-client`, `user-outbox`. They remain **NOT RECHECKED** here. The NodeBB Group sender, local Group fixture and saved direct Person recipient context must be evaluated using current contract fixtures on the locked Node toolchain rather than weakened assertions.

## Requirement assessment

| Requirement | Local evidence | Live acceptance |
| --- | --- | --- |
| R01, direct Community Delete scope | CP37 Gateway syntax and CP39/Community scoped mutation tests | NOT VERIFIED; original `/post/79` signed event unavailable |
| R02, Group mutations, receipts and claims | CP38 migration/DDL + CP39 real SQLite ingress/worker tests; replay, wrong Group, unknown and delayed delivery tested with controlled Discord boundary | NOT VERIFIED; no signed NodeBB Group Update/Delete or actual Discord lost-ACK replay |
| R03, source starter Edit | CP40 actual raw handler and SQLite mapping; empty, partial and mirror scenarios | NOT VERIFIED in Discord |
| R05, title-less Person Note Update | CP41 new Gateway contract test, syntactic validity only | NOT VERIFIED; Gateway dependencies unavailable |
| R06, webhook root attribution | CP42 actual renderer and mocked webhook history | NOT VERIFIED in Discord client |
| R07, command Actor handles | CP44 actual owned Follow/assignment SQLite tests and command syntax | NOT VERIFIED with real Discord slash command |
| R08, Follow notification label | CP45 actual Python follower/DM tests; Gateway shape syntactically checked | NOT VERIFIED on real NodeBB Actor profile |
| R09, link-preserving 2000-unit layout | CP43 real feed renderer, old renderer regressions and boundary tests | NOT VERIFIED with Discord saved content/click target |
| R10, regression gate | Focused Python PASS; full suite and Gateway blocked, no comparable IDs | FAIL / BLOCKED for release gate |
| R11, field repeat | No deployment or peer access | NOT VERIFIED |

## Adversarial self-review

- **Forbidden inputs:** a Group-scoped mutation must prove accepted Group distribution plus immutable owner/ancestry; Group identity must never sign a Person Delete. Event fingerprints reject changed-payload replays, including after later revisions. Actor URL commands check caller ownership again inside existing service transactions. An unproven history entry cannot adopt a Discord delivery or permit a fresh POST.
- **Legitimate inputs:** a valid Group update after subscription, a title-less Person edit, a URL-based offline unsubscribe, and a long but fitting Markdown link must still succeed. Legacy `unknown`/`claimed` attempts without markers remain unresolved rather than being incorrectly adopted.
- **Race boundaries:** a delayed confirmed Create retains its reserved old revision and is due for subsequent Delete; per-target Edit/Delete uses a distinct CAS token from Create. A crash before the atomic receipt/view transaction leaves no new revision, after it retains a terminal fingerprinted receipt. These properties were validated on disposable SQLite/transport fixtures but not under real network cancellation.

**No live v2→v3 migration was performed.** Production/dev backup, exact schema guard and permission checks remain mandatory before any rollout; see `docs/DEPLOY.md`.
