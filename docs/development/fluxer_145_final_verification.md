# Plan 145 — implementation checkpoint and environment-limited verification

**Code base:** `feature/fluxer-support` at `2fd0fdd2`; local implementation checkpoints CP01–CP13. **Scope:** issue #12, exclusive Discord-only or Fluxer-only Bridge Service. **Release status: NOT ACCEPTED.** This is a record of locally implemented code and tests, not evidence of live SDK/REST/Gateway or Docker acceptance.

## Implemented contracts

| Plan | Implementation / observation | Local evidence and remaining limit |
| --- | --- | --- |
| S01–S03 | `BRIDGE_PLATFORM` selects exactly one platform, defaults to Discord; settings reject absent or mixed credentials. | Config and invalid-mode regression tests. |
| S04, S18 | SQLite mode/actor-key preflight is read-only and precedes migration, under lifetime/restore locks; initial Fluxer binding is transactional. | Released-v0, owned-v1, actor-key-only, same-connector restart and mismatch tests. No existing production deployment was mutated. |
| S05, S15–S16 | Fluxer runtime builds no Discord bot, OAuth or personal-feed worker. HTTP, DB, Fedify and authenticated private ingress persist without Node; canonical Fluxer mutation worker and User Actor registration are explicitly unavailable until #13/#14. | Actual Python HTTP startup, SIGTERM and no-Node behavior, negative user registration/Follow and mutation-less scheduler scenarios. |
| S06–S07 | Single Node SDK owner, private authenticated loopback RPC, verified origin/bot ID, executor-admission `gateway_gap` and live SDK-user checks. | Node READY/gap/changed-user tests with real private RPC service and SDK-edge fakes; **real Gateway reconnect not observed**. |
| S08–S12 | Production Node RPC dispatcher now wires the existing SDK read, forum Create, thread Send and bot-owned Edit/Delete executors. Physical scope, forum tag policy, ACL, message and starter IDs are checked without inferring missing receipts. Unsupported operations stay unsupported. | Python→Node crossprocess fixtures, production dispatcher and negative ACL/provenance tests. **SDK package/real server/typecheck not available in this environment; writes are not production-accepted.** |
| S13 | Existing SQLite coordinator owns durable Create/mutation claims; direct adapter mutation has no persisted idempotency. Uncertain response is not replayed automatically by the same direct invocation; coordinator claims remain unresolved across restart. | File-backed SQLite lost-ACK/restart, negative retry, scheduler lanes and direct mutation tests. |
| S14 | All six Gateway event kinds enter authenticated Python ingress as observations only, including duplicate/echo handling. | ASGI/SQLite boundary tests; no Fluxer-originated ActivityPub publication in #12. |
| S17 | Compose selects one platform; sidecar refuses a Discord-mode bridge. Bot token remains Node-only and RPC loopback-only by configuration; documented independent projects/domains/volumes/ports. | YAML/static contract tests only; **Docker engine verification not available**. |
| S19 | Full suites and real Fluxer/Docker acceptance are release gates. | See below: **NOT PASSED as a whole**. The follow-up environment must execute the checklist and attach evidence. |

## Local test evidence and limitations

The initial baseline (before CP02) is recorded in `docs/development/fluxer_145_initial_baseline.md`: full Python, Fluxer Node and Fedify Node runs were blocked by missing locked dependencies. The following are **current** locally observable results:

- `node fluxer-connector/scripts/check-lock.mjs`: **PASS**.
- `node --experimental-strip-types --test fluxer-connector/tests/*.test.mjs`: **115 tests PASS; 2 SDK-dependent test files FAIL TO LOAD** because `@fluxerjs/core` is not installed. This does not establish that the full Node suite passes.
- `npm --prefix fluxer-connector run check`: **BLOCKED**, TypeScript cannot find `@types/node` after an incomplete offline npm installation; `@fluxerjs/core` is unavailable. The TypeScript contract of the production SDK dispatcher is therefore not proven here.
- `npm --prefix fedify-gateway run check`: **BLOCKED**, Fedify package and Node type dependencies absent.
- `node --experimental-strip-types --test fedify-gateway/tests/verify-remote-socket-admission.mjs fedify-gateway/tests/verify-remote-actor-lookup.mjs fedify-gateway/tests/verify-forwarded-origin.mjs`: **6 PASS; 1 file fails to load** because `@fedify/vocab` is absent.
- `python -m unittest tests.behavior.test_fluxer_sdk_reads_crossprocess -q` after updating pre-CP09 assertions: **4 PASS**, including positive verified forum capabilities and refusal to create without a required tag. Combined read/Compose/Gateway Python checkpoint suite: **20 PASS**.
- `python -m unittest discover -s tests/behavior -t . -p 'test_*.py'`: an environment-limited pass initially executed **165 tests**, with 17 missing Discord/discordops import errors and 2 **obsolete pre-CP09 expectations** for disabled write flags. The latter were updated to the new contract and the focused module passed; a second broad run was attempted but exceeded the execution deadline without producing a completed summary. `pytest -q` cannot collect because `hypothesis` is absent, so a complete repository-wide Python pass is **not established**.
- Prior checkpoint-specific tests (CP01–CP12): behavior and SQLite read/write claims, SIGTERM HTTP, Node write gap/identity, production dispatcher and gateway ingress, Python→Node forum Create, Compose static tests were executed and their counts recorded in the respective checkpoint reports. These are fakes at external SDK/network boundaries, not live acceptance.

**Do not interpret** import failures, `skip`, static YAML checks or SDK-edge fakes as successful real acceptance. The branch may not be called production-ready until S19 passes.

## External acceptance checklist (required before #12 completion)

1. Install exact lockfile dependencies using Node 22 and Python >=3.12, then run `uv sync --frozen --extra dev`, `uv run --frozen --extra dev pytest -q`, `npm --prefix fluxer-connector ci --ignore-scripts --no-audit --no-fund`, its `check:lock`, `check`, and `test` scripts; run Fedify `npm ci`, `check`, and `test`. Record package versions and actual results; do not silence baseline failures.
2. Run two **different** isolated Compose projects: Discord-only using historical data and Fluxer-only with a fresh DB, separate origins, volumes, public ports and credentials. Verify namespace, loopback listener, bearer rejection, sidecar-only bot-token secret mount, startup without Discord creds, Node offline, independent sidecar restart and clean full shutdown. Run `dev/verify_fluxer_deployment.py` with an operator-selected test project and capture its result without secrets.
3. On the exact recorded live Fluxer build with approved discovery endpoints, verify Gateway READY, disconnect/reconnect gap and matching SDK bot identity; confirm stale/gap/identity drift cannot write even if Python previously observed READY. Verify read-only availability while write admission is revoked.
4. Physically exercise forum creation with and without `REQUIRE_TAG`, valid/invalid/restricted tags, independently verified starter ID and both equal/distinct thread/starter IDs. Perform root and nested replies with exact immediate parents, blocked/thread-rights-denied paths, bot-owned edit/delete and forbidden foreign/webhook operations. Read back **every** effect through the real SDK/REST endpoint. Record measured server version and receipt shapes, without tokens.
5. With persistent SQLite, test Create parent→child dependency, committed dispatch proof, lost response after accepted POST, restart, reconciliation by positive attempt evidence only, no unproven replay, partial target failures and a recovered sidecar. Validate no duplicate canonical objects and no Fluxer→ActivityPub publishing before #13.
6. Repeat full Discord-only regression suites including actor URLs/signing keys, released-v0 migration, User Actor Follow, Personal Feed replies/threads, community post/comment/edit/delete, subscriptions, local copies and physical mutation renderer. Compare to the recorded baseline rather than declaring pre-existing failures fixed without evidence.

## Release and roadmap boundary

#12 contributes an implemented, locally fake-tested standalone Fluxer transport candidate, **not** a released or live-accepted feature. Do not close #12/PR #25 or advertise physical writes as accepted on this report alone. #14 must supply verified Fluxer author identity before #13 enables communities/subscriptions and canonical mutations; #15 handles active-platform administration; #17 handles final release integration with current `main`. No mixed-platform Bridge Service, shared Discord/Fluxer community, or cross-platform account linking is implemented.

## Adversarial review summary

- **Forbidden accepted?** A stale Python READY cannot bypass Node's final gap/current-user check. Forum required-tag data and target ACL are freshly inspected at SDK boundaries; bot-owned edit/delete require exact scope/provenance. Real SDK may differ from fakes: verify against the pinned server before acceptance.
- **Valid rejected?** Deliberate fail-closed paths reject unknown/missing forum metadata, ambiguous physical receipt, unavailable optional registration and pre-owned Discord identity files. These are explicit constraints, not silent coercions.
- **Invariant before/after boundary?** DB instance-mode preflight executes before migration/key creation; transactional connector admission remains authoritative after migration. Node write identity/gap check executes before executor call. A write admitted before an SDK disconnect remains uncertain and cannot be rolled back; SQLite recovery fences unproven second dispatch.
- **Regression gaps:** Full Discord test suite, real SDK and Compose require external dependencies; do not infer their success from the current environment.
