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
without overwriting it. Generic AP comment mappings and sibling Discord thread/message surfaces
also compare exact identity under the writer reservation, recovering after a partial handler
failure. Early duplicate ingress now checks current subscription, persisted canonical
identity and available source evidence before fanout/relay; comment replay can
repair a lost generic mapping from the saved host receipt. The R5-F1 Fedify
nested-Create source authentication gate is not implemented, so stored raw
source JSON cannot yet be treated as cryptographic author proof. Runtime adapter routing and all mutation/SDK-specific ingress checks also remain
incomplete. Physical adapter SDK I/O now tracks task lifetimes for reads, edits,
deletes and creates; quiesce cancels and joins them after inbound callbacks drain,
but this does not prove command/interaction callback lifetime safety.

## Stage 03 outbound signed JSON egress hardening (checkpoint 12, partial)

Custom signed ActivityPub POST now admits the HTTPS URL before signing,
validates every DNS result at the actual TLS connection lookup and rejects
private/special-use IPs. The Node HTTPS transport does not follow redirects,
uses a fresh socket and bounds response size/time. Three standalone Node tests
and isolated TypeScript checking pass. The Fedify-native `sendActivity` path
and unrestricted remote Actor fetch are **not** covered by this change; the
Fedify dependency lockfile remains at 2.2.0 because the package registry is
unreachable. This is not completion of the R7-F2 security release gate.

## Stage 03 remote Actor GET hardening (checkpoint 13, partial)

Remote community Actor resolution now uses Fedify `ctx.lookupObject` with
`crossOrigin: 'throw'` and validates exact Actor ID and public HTTPS inbox.
The previous unrestricted `fetch()` in `outbound/remote-actor.ts` is removed;
all Follow/publish/mutation call sites pass their federation context. Three
positive/negative Node tests pass without Fedify dependencies. Full Fedify
runtime and dependency-version checking remains blocked by unavailable npm;
this is not final R7-F2 integration verification.

## Stage 03 forwarded ActivityPub origin gate (checkpoint 14, not release-certified)

Wrapped Announce(Create/Update/Delete) now compares cached outer identity to
Fedify's typed accepted Announce and dereferences the exact original activity
via its origin-checked lookup before normalization. Python receives only the
independently verified payload. Direct activities check typed Fedify actor vs
embedded author. Eight pure security scenario tests pass. This gateway path has
**not** run with installed Fedify, and the `package-lock.json` still pins
vulnerable Fedify 2.2.0. Legitimate non-dereferenceable forwarded mutations
are explicitly fail-closed under T9. Real Lemmy federation and DNS/redirect
integration are mandatory release gates.

## Stage 03 checkpoint 14 focused regression verification

With global `conftest.py` disabled solely to avoid baseline missing `hypothesis`,
51 Stage 03 standalone Python scenarios pass (SQLite/fake SDK). Four standalone
Node source modules typecheck with global Node types, and 14 Node security
scenarios pass. Full pytest, Fedify TypeScript suite and live Discord/Fedify
integration are not verified, and the vulnerable Fedify dependency remains a
release blocker.

## Stage 03 typed message mutation admission (Checkpoint 16, 2026-10-19)

The existing mutation handler now supports typed edit/delete events with connector
validation before Discord-ID DB lookup, no SDK message objects in the typed
path, echo/role admission, and adapter-only reconciliation for partial edits.
The production bot remains on its legacy callback path until the full one-ingress
cutover can switch thread/message creation and mutations atomically. Five
focused positive/negative tests plus eleven existing mutation-handler tests pass
using an import-only `discord` test stub; this does not verify the real SDK.

## Stage 03 checked legacy ID projection (Checkpoint 17, 2026-10-19)

`discord_id_to_legacy_int()` now accepts only `discord-primary` platform
references, rejects zero/invalid snowflakes, and distinguishes thread/parent
and message/container IDs. Typed edit/delete admission uses this boundary
instead of direct integer parsing. Nine positive/negative projection tests pass;
remaining community runtimes still require the same cutover in Stage 03.

## Stage 03 uncached thread-parent normalization (Checkpoint 18, 2026-10-19)

The Gateway normalizer rejects a message with a physical `parent_id` but no
verified parent object, avoiding accidental Personal Feed root publication.
A real SDK caller must fetch/verify the parent before normalizing; no callback
cutover has yet been performed. Two targeted positive/negative tests pass.

## Stage 03 Gateway ingress activation (partial)

- Raw Discord message edits/deletes and raw thread updates/deletes now use one
  adapter-normalized ingress; the old raw mutation entry points are no longer
  called from production Gateway callbacks. Message/thread creation is still on
  SDK-object handlers until the single typed Create cutover is complete.
- Full pytest remains blocked by missing `hypothesis` and `discord.py`; the
  installed Fedify dependency and native SSRF transport remain unverified.

## Stage 03 worker shutdown order (verified focused tests)

- `app._run_bot_lifetime()` now stops Personal Feed and User Actor workers
  before Discord SDK close; one worker stop error cannot skip the other.
  Adapter callback/SDK-task drain and both worker stops join cooperative async
  finalizers even after a second cancellation. Full installed-SDK shutdown and
  uncooperative network integration tests remain unverified.
- Definitive Personal Feed Discord preflight errors (`invalid_request`,
  `unsupported`) now stop retries using existing failed state; transient
  `unavailable` still schedules recovery.

## Stage 03 Personal Feed root DTO ingress (Checkpoint 21, 2026-10-19)

- Bound text/announcement roots now pass through DiscordAdapter, the single typed ingress and the owner-verified PersonalFeedPublisher. Production forum starters and thread replies still use legacy SDK-shaped create routes; submitting those through typed ingress fails visibly. The legacy `publish_root(message=SDK, binding=...)` entry remains for tests and other direct callers until the remaining Create cutover is complete; it shares the same persistent publish core.
- Original Discord source timestamp and owner binding remain required. Unsupported attachments are rejected explicitly. Focused real SQLite tests pass, while full installed-SDK tests remain blocked by missing dependencies.

## Stage 03 Personal Feed typed discussion-thread adoption (Checkpoint 22, 2026-10-19)

- The live manual text/announcement discussion-thread path now normalizes `ThreadCreated`, checks the persisted Personal Feed root identity and exact parent, and adopts using the existing repository. A verified forum `ThreadCreated` cannot silently pass the incomplete typed Create path. Missing/wrong parent or guild is rejected at adapter boundary; no starter history guess is made for Personal Feed.
- Generic forum thread and local/remote community message Create remain SDK-shaped. Full installed-SDK and multi-service integration tests remain unavailable; focused normalized runtime/SQLite tests passed.

## Stage 03 verified thread message boundary (Checkpoint 23, 2026-10-19)

- Adapter now verifies observed thread parent and three guild identities before normalizing a reply into `MessageCreated`; synthetic starter events and unsupported attachments cannot publish through that boundary.
- Production `on_message` remains on SDK-shaped forum/Personal Feed reply handlers until typed domain routes can be switched without duplicate publication.

## Stage 03 Personal Feed typed replies (Checkpoint 24, 2026-10-19)

- Production Personal Feed reply Create now uses `DiscordAdapter` → normalized ingress → canonical SQLite publication, including out-of-order root adoption and nested-parent validation. Legacy direct `publish_local_reply(message=...)` remains for existing non-production callers until the remaining forum cutover.
- Cold-cache thread parent resolves through an exact adapter channel lookup. If the lookup fails, ingress rejects the event without fabricating a parent.
- Community/local forum Create and fanout are still SDK-shaped. Full installed-Discord/Fedify integration tests and patched Fedify native SSRF validation remain blocked by missing dependencies.

## Stage 03 normalized forum starter publisher preparation (Checkpoint 25, 2026-10-19)

- `ContentPublishService` now has an SDK-free forum-starter publication entry that checks a verified `ThreadCreated` and `MessageView`, registration, moderation and distinct thread/starter IDs before calling the unchanged AP publication/persistence core. Real SQLite positive/negative scenarios pass with fake external transports.
- This is **not** a production cutover: subscribed/local community forum callbacks, ownership, sibling fanout and starter acquisition still use existing SDK-shaped handlers. Do not activate a second publish path until those boundaries move together.

## Stage 03 normalized forum comment publisher preparation (Checkpoint 26, 2026-10-19)

- `ContentPublishService.publish_normalized_comment_to_community()` now requires verified forum parent and same-thread reply references, checks author registration/moderation, and delegates ActivityPub comment rendering/persistence to the same existing fields-based canonical core. A failure to send rejection notices cannot federate the comment. Real SQLite positive/negative tests pass alongside legacy post/comment publishing tests.
- The new API takes an AP parent object ID selected by the owning runtime. It is **not** a replacement for canonical thread/reply-group ownership validation; no production community callback has been switched, and Community/Local Community fanout remains SDK-shaped.
