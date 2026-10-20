# Known issues

- Stage 01 review fixes: typed claims reject incompatible channel kinds before
  owner FK updates; `create_all()` remains schema-only and app fixtures use
  `migrate()`. Mixed Accept, local fanout, dashboard, listing and Personal Feed
  workflows have runtime regressions. Personal Feed channel admission also
  checks the requested user, preventing cross-user channel collisions.

- Current-v1 admission compares the complete generated-role column definition with model DDL. Appended expressions and wrappers are rejected; STORED mode remains required.
- Current startup upgrades the recognized v0.1.0 release schema directly from 0 to 1 in one transaction. Unknown release variants and unpublished development schemas are rejected unchanged; the actual deployment database has not been supplied for recognition. Released actor/object namespaces now cut over inside that transition, with only affected federation confirmations reset. Discord bindings/local subscribers and signing material are preserved; missing identity settings or unsafe signed-payload edits fail closed. Source collection/key ownership and canonical local content grammar are validated before writes; foreign metadata, unknown local formats and canonical IDs contradicting an explicit publication/outgoing-mapping/relay owner abort unchanged. Incoming relay actors and thread/group community context are not treated as object authors. The unused alternate schema helper mechanism has been removed. Plan 147 replaces internal Stage 00 reconstruction with SQL TEMP staging and a single ORM target creation. Narrow identity/relationship evidence excludes bodies, keys and untouched audit data; native SQL preservation covers all staged values. Advisory preflight/post-change validation remain separate. Targeted cleanup removes the historical migration probe/model fixture. Supported-release scenarios execute production migration; current-only descendant restart/cascade checks run on v1. The renamed Stage 00 SQL fixture is used only to assert rejection. Native reference-schema comparison replaces manual model/column/CHECK parsing, and released scalar links/channel inventory are explicit SQL.
- Bridge-local bans leave existing remote subscriber rows unchanged and do not emit ActivityPub moderation activities.
- Remote handle resolution is string-only; WebFinger and actor fetch resolution are not implemented.
- A globally banned super-admin cannot unban themselves through Discord commands. Recovery requires another non-banned super-admin or direct database intervention.

## Local-community relay outcome persistence is not atomic

Gateway outcomes are applied one row at a time through repository methods that open
separate sessions. If persistence fails after one outcome is committed, earlier
rows remain updated while later rows remain pending. Pending rows are retryable,
but the fanout call itself raises and does not return a complete summary.

## Personal-feed history is not replayed after rebinding

Inbound personal-feed comments remain durable when the active binding is removed,
moved, or Discord delivery reaches its three-attempt terminal failure. The bridge
suppresses delivery to stale channels but does not reconstruct retained roots,
threads, or comments in a replacement Discord channel.

## Stage 01 simplicity review

- Personal Feed service lookups now share `discord_claim_query()` with the repository; mismatched guild/channel projections are excluded consistently.
- Migration parent delivery lookup uses snapshot indexes keyed by group ID and exact target channel/thread; missing parents still abort and roll back the upgrade.

- Migration diagnostics now provide SQL operation/table/check context with chained causes and hidden bound parameters. Conflict errors expose selected IDs only; phase logs report transaction outcomes without row bodies, keys or tracebacks. Historical v1 target pinning is tracked before any v2 ORM change.

## Stage 02 implementation review (resolved)

- `validate_created_receipt()` now rejects conflicting known forum kind/guild
  metadata independently of physical `ChannelRef` equality. Missing metadata
  may still be enriched without changing the channel identity.
- The Stage 02 stateful test transport now assigns distinct, deterministic
  thread/starter IDs for independent forum posts and does not replace an
  already observed thread. This was a fake-only defect, not live Discord routing.

## Stage 02 adversarial checks (resolved)

- Normalized inbound message events now reject contradictory known kind/guild
  observations between parent and thread contexts. The explicit event guild is
  checked against all available contexts, not only the first.
- The Stage 02 stateful test adapter checks the actual thread parent during
  fetch/send, so a thread ID cannot be used through a different forum.
- Message edit timestamps and all inbound receipt times must be timezone-aware
  datetimes when supplied; missing optional edit time remains `None`.
  `THREAD_STARTER_MESSAGE` cross-container references remain representable.

## Stage 03 partial implementation validation (2026-10-19)

- The baseline full pytest invocation stops in `tests/conftest.py`: `hypothesis` is missing;
  `uv sync --frozen --all-groups` cannot reach the configured package registry.
  `discord.py` and local Fedify Node dependencies are not installed.
- The Stage 03 Compose gateway secret allowlist is in place and checked statically
  with Node. A live Docker Compose environment-key check remains unverified.
- Normalized source timestamps and edit author metadata are added to Platform API.
  Personal Feed timestamp ordering now normalizes aware incoming timestamps to
  UTC, rejects naive values with `invalid_timestamp`, and interprets SQLite
  naive stored values as UTC. Historical values originally persisted with an
  unknown non-UTC offset remain ambiguous; no inferred migration is applied.
- The live DiscordAdapter ingress/cutover, revision-safe worker operations,
  Fedify security upgrade/origin verification, relay identity/routing and SSRF
  gates are still **not implemented or release-tested**. Stage 03 must not be
  treated as complete based on the focused tests of the partial changes.

## Stage 03 relay transaction changes (partial, 2026-10-19)

Source-level Announce ID reservation and per-attempt inbox/generation persistence
are implemented. Eight isolated file-SQLite regression scenarios pass without
Discord SDK; a direct fake-gateway fanout exercise confirms two recipients share
an ID and a duplicate Create does not re-send. Existing complete relay/runtime
regressions remain unverified because `discord.py` and `hypothesis` are missing.
The historical mixed-ID conflict is intentionally blocked for reconciliation,
not silently re-keyed. Dependency/security and adapter migration remain open.

## Stage 03 Personal Feed claimed-revision and create recovery (partial, 2026-10-19)

The repository now compares the actual claimed revision/deleted flag on every
physical completion. An uncertain ID-less first send remains parked across
restart, Update, Delete and binding suppression until a verified receipt exists.
Five dedicated SQLite scenarios pass; 19 Personal Feed scenarios also pass when
an isolated fake `discord` module stands in for the unavailable SDK import.
The **live** worker still uses discord.py directly and must be cut over to
DiscordAdapter before this Stage can be called complete; real Discord HTTP
errors and cancellation need SDK-backed regression tests.

## Stage 03 Gateway normalization (partial, 2026-10-19)

A pure SDK-free normalizer for six Discord event types now has eight positive/
negative contract scenarios. Production bot callbacks are **not** yet wired to
it, so historical direct SDK routing remains active until the DiscordAdapter
and one normalized ingress replace it. These tests are contract checks, not
live Discord Gateway integration.

## Stage 03 Discord physical transport (in progress)

- `DiscordAdapter` now implements the Stage 02 physical operations with strict
  connector/parent validation, preflight ACL, bot-only presentation, and one
  retained process-local CreateAttempt receipt per immutable request. Stateful
  fake SDK tests cover duplicate POST prevention and actual forum/message effects.
- The actual `BridgeBot` event handlers and domain fanout/workers have not yet
  switched to the new adapter. SDK-dependent integration remains unverified
  without installed `discord.py`; a process-local receipt ledger cannot provide
  cross-process or cross-restart exactly-once delivery (Stage 04).

## Stage 03 Gateway callback lifetime (partial, 2026-10-19)

The live Discord adapter is now constructed/registered from `app.py` and
attached to `BridgeBot`. Existing Gateway callbacks enter one task admission
gate before DB/Fedify access; `run_bot` drains those tasks and stops the
Discord delivery worker inside the SDK context before SDK shutdown. Runtime
alone closes Fedify. The existing SDK-object domain ingress has not been
replaced with `PlatformEventIngress`, and application-command callbacks need
additional lifetime admission review before calling this fully complete.

## Stage 03 idempotent canonical receipt persistence (partial, 2026-10-19)

The existing local-community repository now serializes canonical host thread and
message receipt persistence through `BEGIN IMMEDIATE`. An identical concurrent
receipt returns the original row and a changed AP/physical identity fails
without overwriting it. Generic AP comment mappings also compare both unique
identity keys under the writer reservation, recovering after a partial handler
failure. Source/mirror fanout and early duplicate ingress still require the
separate Stage 03 routing and provenance checks before side effects.
