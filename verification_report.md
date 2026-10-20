# Discord Adapter verification — PR #20

Date: 2026-10-20. Branch: `issue/10-discord-adapter`, base commit
`05ef10427d91caf4c3dbd8e9ff621d7c169886dc`. All edits and tests ran in
`/tmp/discord-adapter-verification-20`, a detached Git worktree. The original
branch checkout stayed clean. No production token, `.env`, database, network,
volume, container operation, push, merge, or deployment was used.

## Result

**NOT VERIFIED for production release.** The targeted security and runtime
regressions pass, but the full suites do not. The Fedify upgrade adds three
failing isolated Node fixture files, and signed public-peer inbox delivery,
live Discord Gateway/HTTP, redirect/TLS behavior over an actual public HTTPS
connection, and cross-process Create receipt recovery remain unverified.

## Changes

- Pinned `@fedify/fedify`, `@fedify/hono`, and `@fedify/vocab` to 2.2.19 and
  refreshed the lockfile. Clean `npm ci` succeeded. `npm ls` confirmed all
  three packages plus `@fedify/vocab-runtime` and `@fedify/webfinger` at 2.2.19.
- Fixed a high-priority Announce cache collision: raw inbox bodies are captured
  before authentication and cached by caller-controlled outer ID. A later body
  with the same ID and actor could replace the nested Create/Update/Delete
  considered by the handler. The verifier now compares that nested ID with
  `Announce.objectId` from the Fedify-accepted typed activity before origin
  dereference. The accepted identity type makes this input mandatory.
- Added regression scenarios for all three nested mutation types, Fedify's
  installed parser, and semantically equivalent author records with different
  JSON property order and timestamp offsets. Updated federation behavior docs
  and the known-issues journal.

## Test evidence

| Check | Baseline | After changes |
| --- | --- | --- |
| Full pytest | 1728 passed, 24 failed, 3 warnings | 1728 passed, the same 24 failed, 3 warnings |
| `npm test` | TypeScript and 14 security tests passed; stopped at `verify-follow-activity-id.ts` | TypeScript and 19 security tests passed; same first failure |
| Independent `verify-*.ts` files | 7 failing files | 10 failing files |
| Focused adapter/SSRF/Remote Community/Personal Feed pytest | — | 94 passed |
| Focused Local Community/fanout/dedup/receipt/worker pytest | — | 69 passed |
| New Announce collision tests before fix | — | 3 failed as expected |
| Announce/security boundary tests after fix | — | 19 passed |
| `npm ci`, `npm run check`, `git diff --check` | — | passed |

All 24 Python failed test IDs match exactly before and after (`diff` of pytest
failure summaries is empty). Baseline and final logs are listed in the worklog.
The seven pre-existing Node failures are `verify-follow-activity-id`,
`verify-local-community-canonical-key`, `verify-local-community-publish`,
`verify-local-community-relay`, `verify-local-community-update-delete`,
`verify-signed-json-delivery`, and
`verify-user-actor-profile-update-delivery`. The three additional failures
after Fedify 2.2.19 are `verify-public-community-follow-workflow` and
`verify-zz-public-user-follow-workflow` (accepted local inbox request, no
`local.follow_requested` event delivered to the test Python boundary), and
`verify-user-canonical-actor` (HTTP 406 rather than expected 404). The two
Follow fixtures stub global fetch and use synthetic `remote.example` actors;
they cannot establish whether a real publicly resolvable peer succeeds.

## Security and integration limits

- Origin proof: the helper compares nested Create/Update/Delete identity,
  attribution, recipients, parent and supported content against a separate
  author-origin dereference. The new typed ID binding prevents a different
  nested activity being substituted under one accepted outer ID. A full
  signed HTTP inbox exploit test was not completed. Same-ID content changes
  between receipt and author dereference have not been proved safe under every
  remote profile.
- SSRF: local boundary tests reject private/special addresses, mixed DNS
  answers and loopback signed-delivery targets. A standalone adversarial run
  forced the production Python fetch path to resolve `remote.example` to a
  newly started isolated loopback server; it raised before dial and the canary
  observed zero connections. Real public TLS handshake, certificate failure,
  and HTTP redirect behavior end-to-end are **NOT VERIFIED**. Existing tests
  inspect TLS/redirect options and use fake HTTP boundaries.
- Discord: `discord.py` 2.7.1 was installed; 163 focused Python runtime/SQLite
  scenarios passed with fake outer SDK/network edges. Actual Discord Gateway
  and HTTP operations were **NOT VERIFIED**, since no safe test account/token
  was available and production use was prohibited. Process-local
  `CreateAttempt`/unknown-delivery behavior is covered; durable cross-restart
  recovery for arbitrary forum/message creates remains outside the current
  adapter contract.
- `npm audit --omit=dev` reported five unrelated Hono/OpenTelemetry findings
  (one high, four moderate). Fedify 2.2.19 had no audit finding in this run.
  They were not changed under this task's dependency scope.

## Production isolation

Read-only `docker ps` snapshots before and after testing contain the same 15
container IDs, names and `running` states, including every bridge/gateway
container. Their uptime increased from about 30 to 31 hours with no restarted
ID. The agent issued no Docker mutation command. These observations support
that the two production containers were not stopped or restarted; their
internal data was deliberately not inspected.
