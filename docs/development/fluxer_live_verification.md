# Fluxer issue #12 — acceptance evidence and live verification

**Recorded on:** 2026-10-29. **Status: BLOCKED**, not accepted as production
transport. This report records observed local evidence without assigning
unperformed external/real-instance checks a PASS result.

## Current corrective-series evidence

The original CP034 environment snapshot below is historical. The installed SDK,
lockfile, ordinary TypeScript contracts and local runtime regression status have
since changed; see [CP035–CP041 verification](fluxer_fix_verification_ru.md).
Docker/real-instance acceptance remains BLOCKED. In the corrective series CP035
means runtime lock safety; the original plan's real-instance CP035 exit gate is
now referred to as the separate live acceptance stage, and has not been passed.

## Historical CP034 environment snapshot

## Target and immutable acceptance boundary

- Repository baseline: `1645689c63601ceea8b7ad98b2ab0a023b4da160`.
- Ordered patch set: CP001–CP033 applied; CP034 is regression/documentation.
- Requested official SDK: `@fluxerjs/core@3.3.0` in
  `fluxer-connector/package.json`. **Installed SDK: none**. **Verified
  package-lock: none** (npm registry DNS `EAI_AGAIN`). An SDK package version
  in a manifest is **not** proof that a published SDK artifact was tested.
- Target real Fluxer **server tag/commit: not supplied**. No actual bot token,
  approved live instance, test forum, tag policies, Gateway capture or
  credentials were available. No real outbound REST write was attempted.
- Docker/Compose CLI: not installed here. The two-container topology was
  statically checked only, with five Python tests; no live namespace/listener
  or secret-mount inspection occurred.
- The only current production Node `main.ts` operations are the verified
  read/permission/capability handlers. Create/Send/Edit/Delete remain
  `unsupported` intentionally. Python `FluxerInboundHandler` retains
  `MULTIPLATFORM_FLUXER_COMMUNITY_BINDING_PENDING` (issue #13), and verified
  Fluxer Gateway Create/Update/Delete yields zero canonical AP publications.

## Local automated test observations

| Invocation | Observed result | Interpretation |
| --- | --- | --- |
| `PYTHONPATH=.:tests python -m unittest -q <26 selected Fluxer/platform modules>` | **142 tests OK; 1 skip**, 2026-10-29 | Available fake-provider, real SQLite, interprocess ownership and transport contracts pass. The skip is the real app composition module requiring `discord.py`. |
| `node --experimental-strip-types --test fluxer-connector/tests/*.test.mjs` | **106/106 PASS** | Contract fakes and Node/private HTTP service, not the installed SDK. |
| `python -m unittest -q tests.behavior.test_fluxer_compose_topology` | **4/4 PASS** | Static YAML, secret/port/profile constraints and lockfile-gated release wiring; **not** B20/B21. |
| `python -m compileall -q src tests` | **PASS** | Python syntax only. |
| `git diff --check` / `git diff --cached --check` | **PASS** | No whitespace or patch-format errors. |
| `python -m pytest -q` | **BLOCKED** at `tests/conftest.py`: missing `hypothesis` | No complete Python suite was collected. |
| Same `python -m pytest -q` on exact original Git HEAD | **Same missing `hypothesis` failure** | A verified environment-baseline blocker, not evidence of a new regression or evidence that the suite passes. |
| `npm --prefix fluxer-connector run check` | **BLOCKED**: missing `@types/node` | Published SDK TS declarations have not been compiled. |
| `npm --prefix fedify-gateway test` | **BLOCKED**: Node dependencies absent | The **original HEAD** run also failed with missing Node declarations/modules. |
| Actual `docker compose --profile fluxer ...` | **NOT RUN**: Docker absent | B20/B21 are unverified. |
| Pinned server REST/Gateway acceptance | **NOT RUN**: instance/credentials/commit absent | CP035 and issue #12 exit gate are unverified. |

No failing existing suite test was removed, rewritten, or re-baselined to claim
success. No new migration was introduced: database target version remains 1.

## B01–B21 cross-boundary scenario classification

`LOCAL` means the corresponding deterministic fake or SQLite behavioral path
ran in the selected Python or Node tests; it never substitutes for a live
end-to-end `PASS`. `PARTIAL` indicates that required runtime/provider
boundaries were not executed. All scenarios requiring a real server or
containers must remain open regardless of local evidence.

| ID | Local evidence and remaining gate | Status |
| --- | --- | --- |
| B01 | Token-free disabled setup / config tested; complete `src.app.build_runtime` integration requires `discord.py`. | PARTIAL |
| B02 | SQLite target-v1/unique-index/v0 conversion tests ran; post-migration Discord/AP activity was not run with full app. | PARTIAL |
| B03 | One trusted connector, origin/key conflicts and rejection covered by selected tests; live SDK-start negative cases and actual app composition unavailable. | PARTIAL |
| B04 | Fake approved discovery/SDK reads/permissions and RPC covered; published SDK, real REST redirects, TLS and server build unavailable. | PARTIAL |
| B05 | Test-only fake SDK/HTTP forum Create and distinct starter/receipt covered; live forum POST not performed. | PARTIAL |
| B06 | Fake nested replies and parent validation covered; live message/tag/mention behavior not measured. | PARTIAL |
| B07 | Bot-identity and foreign/webhook ownership negatives plus fake SDK mutations covered; production handlers still refuse writes. | PARTIAL |
| B08 | Unsupported webhook and pre-write refusals tested with fake SDK; real server ACL/tag enforcement not tested. | PARTIAL |
| B09 | SQLite `unknown_delivery` + lost fake response scenarios ran; real response-loss/reconciliation not induced. | PARTIAL |
| B10 | Node SDK-model event → authenticated HTTP → SQLite claimed/unclaimed gate tested, including uncached parent; real SDK events absent. | PARTIAL |
| B11 | Partial/ID-only DTO ingress and claim no-op exercised; no live edit/delete stream. | PARTIAL |
| B12 | Fake Gateway reconnect/late-ingress/shutdown tested; real reconnect/resume and app shutdown blocked. | PARTIAL |
| B13 | Offline Fluxer pending/no-claim and independent Discord completion exercised on SQLite with test adapters. | LOCAL; live pending |
| B14 | Event-barrier connector-lane independence exercised with fake providers; real blocked Fluxer socket vs Discord not tested. | LOCAL; live pending |
| B15 | Single mutation CAS claim, unclaimed sibling and fairness tests exercised against SQLite; production live crash not induced. | LOCAL; live pending |
| B16 | Parent receipt/child recovery and fake-provider state exercised; full app and live forum acceptance pending. | PARTIAL |
| B17 | Connector scoped origin/IDs and second instance refusal exercised; production Discord/AP scenario pending. | PARTIAL |
| B18 | Actual separate-process kernel `flock` tests ran; complete Docker runtime backup/restore/start requires application dependencies. | PARTIAL |
| B19 | Full registered-user Follow → Discord post/edit/delete/AP echo regression not executable without dependencies. | BLOCKED |
| B20 | YAML topology tests only; no Docker Compose two-container RPC/Gateway/restart evidence. | BLOCKED |
| B21 | Source-level Node-only token/secret constraints only; no actual container env/mount inspection. | BLOCKED |

## R01–R24 invariant verification register

No invariant is declared fully accepted unless its prescribed authoritative
boundary and required negative test were exercised. The IDs retain the
canonical plan's meanings; this table is a coverage audit, **not a substitute
for the original R01–R24 definitions**.

| IDs | Evidence available | Missing required proof |
| --- | --- | --- |
| R01, R02, R20 | Fresh SQLite target v1, canonical origin/index, single key config and negative SQL collisions in focused tests. | Populated released-v0 real fixture + full Discord runtime after conversion, actual SDK startup refusal. |
| R03, R04, R06 | Fake SDK starter/thread/reply/uncertain attempt with SQLite receipts. | Real pinned forum API, independently witnessed starter, response-loss replay. |
| R05, R17–R19, R21 | SQLite/adapter connector-scoped lane barriers, offline preflight, slot-claim fairness, pending/reconcile tests. | Production SDK I/O blocked alongside real Discord; restart/stop integration with installed SDK. |
| R07, R10, R13 | Fake SDK events normalized and forwarded via authenticated DTO/SQLite; AP gating and session diagnostics tested locally. | Real Gateway payloads, disconnect/reconnect/resume/gap on pinned build. |
| R08, R09, R15, R16 | Existing persistence and explicit Discord/Fluxer scope checks were exercised in selected tests. | Full Discord/AP signed-activity, User Actor/Follow and physical edit/delete baseline suites. |
| R11, R12 | Fake SDK ACL, bot ownership, webhook-negative, mention and attachment refusal tests. | Live rights/tags/attachments/mention behavior. |
| R14 | Unit/HTTP ingress admission, task drain and async read/write cancellation fakes. | Real app shutdown while external callbacks/SDK tasks are pending. |
| R22 | Real multiprocessing/spawn and POSIX `flock` exclusion, aliases and separate DBs in selected tests. | Full app startup through EX→SH barrier, backup/restore with real live bridge SDK. |
| R23, R24 | Five static Compose tests and Node-only token reader/service fakes. | Actual two-container Compose network/secret/health/restart tests (B20/B21). |

## Required real-instance acceptance sequence (not executed)

Provision an isolated, operator-controlled instance and record its immutable
server commit/tag, approved discovery/REST/Gateway hostnames, SDK npm integrity
metadata and a disposable forum/channel/guild plus a Node-only bot credential.
Run the actual Compose topology after producing/reviewing a reproducible npm
lockfile and verifying SDK TypeScript declarations. Redact all secrets,
content and nonpublic hostnames from resulting evidence.

1. Record tokenless discovery `/.well-known/fluxer`, `endpoints.api_public`,
   exact approved Gateway host and authenticated Node SDK bot identity; reject
   unexpected hosts, redirects and HTTP downgrade. Inspect signed/no-secret
   Python process environment and the Node-only secret file mount.
2. Fetch a real forum and tag-required forum with actual permission/member/
   overwrite data; confirm thread enum, guild, forum parent and per-action
   bot rights. Capture SDK wire method/payload and response for one forum POST
   and separately identified `(forum, thread, starter)`—do not infer identity.
3. Verify ordinary/nested replies, mentions, required tags, rejected media,
   bot-own Edit/Delete and denied foreign/webhook attempts against the exact
   stored `MessageRef`. Inspect physical forum state and SQLite receipt/revision.
4. Capture each actual Gateway create/update/delete and thread create/update/
   delete, including uncached/partial payloads and reversed event ordering.
   Exercise reconnect/resume and invalid-session diagnostics. Prove the
   preserved issue-#13 canonical gate emits zero AP activities.
5. Commit one fake-for-live disposable remote Create while deliberately losing
   the HTTP response, restart Node/Python in a controlled scenario, and show
   no second POST without positive attempt-correlated proof. Stop with an
   admitted callback pending and verify no post-SQLite-close work.
6. Run two real Compose containers in a shared namespace; inspect the
   127.0.0.1 RPC listener and non-published port, bearer rejection, Node-only
   mount and Python env, sidecar restart without Discord interruption,
   bridge recreation/namespace rejoin, port collisions and stop order.
7. Re-run the entire Python and both Node package suites, SDK TS checks,
   `git diff --check`, complete B01–B21 and R01–R24 matrix and independent
   architecture review. Classify failures against original HEAD and record
   request/response timings and physical/SQLite/operator results.

**Activation rule:** only after confirming these exact contracts on the
recorded build may the already isolated `forum_create.ts`, `message_send.ts`
and `message_mutations.ts` handlers be connected to the production
`main.ts` dispatcher and their truthful capabilities advertised. No
reconciliation by message title/body or unsafe second POST is permitted.
Absent this evidence, leave production writes unsupported and #12 **BLOCKED**.
Issue #13 connector-aware canonical community binding and #14 user linking
remain explicitly outside this acceptance.

## Adversarial self-review of the locally implemented boundaries

- **Potentially forbidden acceptance:** the shared-namespace Python endpoint is
  bound to the existing `0.0.0.0` listener, not a private listener. Auth must
  therefore be checked for every Fluxer event before parsing and connector
  association; reverse proxies must not expose `/internal/fluxer/events`.
  A review of Compose must not claim host-level traffic isolation based only
  on `network_mode: service:bridge`. The read-only SDK dispatcher must not be
  interpreted as a production Create capability, even when `health` is READY.
  Docker/real provider verification of this boundary remains open.
- **Potentially valid rejection:** a verified server may support forum
  posts/replies that the current read-only production dispatcher rejects.
  That rejection is intentional while real operation-specific tags,
  permissions, thread/starter identity and SDK types are unmeasured. No
  fabricated fallback/guessed starter is authorized to avoid it. Existing
  Fluxer forum ownership never enables source AP publication until issue #13.
- **Boundary timing and lifetime:** offline readiness is checked before any
  SQLite Create claim; cancellation after the adapter method is entered may
  have caused a remote effect and must retain the uncertain durable fence.
  Node service restart may invalidate readiness/bot identity but must not
  authorize a second POST. Runtime lifetime ownership must enclose recovery,
  HTTP handlers, adapter work, SQLite and the EX→SH deployment-lock handoff.
  Unit/provider-fake tests cover portions; full `src.app` shutdown and real
  SDK socket cancellation require the blocked external acceptance checks.
- **Credential packaging:** the actual Node-only token is a separate file and
  is now excluded from both Git (`/secrets/`) and Docker build context;
  checking in `.env` or a token-containing Fluxer connector JSON remains
  forbidden. The shared IPC bearer is not the bot token. No token may be
  printed into error/health responses, logs or SQLite.
- **Release reproducibility:** no `npm install` fallback is permitted when a
  reviewed lockfile is absent. Optional image publication is conditional on
  a lockfile check performed **after** `actions/checkout`, through an explicit
  `validate-version` output; it cannot be reliably based on a job-level
  workspace `hashFiles` expression evaluated before checkout. Current image
  build remains intentionally blocked.
