# Known issues

- Verified: bridge-local bans do not remove existing remote subscriber rows and do not emit ActivityPub moderation activities.
- Verified: remote handle resolution remains string-only; WebFinger/actor fetch resolution is not implemented.
- Fixed/verified: Stage 4 startup migration now repairs retained community-sync group AP IDs and same-origin local community actor IDs in `community_thread_groups` and `community_message_groups`.
- Fixed/verified: local-community Discord -> Lemmy publish no longer crashes when the gateway client serializes Stage 5 publish requests without optional mutation metadata; the regression was introduced by commit `f0d7eb4`.
- Known operational behavior: a globally banned super-admin cannot unban themselves through Discord commands; recovery requires another non-banned super-admin or direct database intervention.

## Local-community relay outcome persistence is not atomic

Gateway outcomes are applied one row at a time through repository methods that open
separate sessions. If persistence fails after one outcome is committed, earlier
rows remain updated while later rows remain pending. Pending rows are retryable,
but the fanout call itself raises and does not return a complete summary.
## Personal-feed history is not replayed after rebinding

Inbound personal-feed comments are retained even when the active binding is removed, moved, or Discord delivery reaches its three-attempt terminal failure. Stage 8 deliberately suppresses synchronization to stale channels and does not reconstruct retained roots, threads, or comments in a replacement Discord channel. Restore/replay tooling remains future work.

- Personal-feed comments tied to an inactive or removed binding remain durable but are not rendered into Discord.

## NodeBB release branch baseline (2026-11-11)

- Fixed through CP06: repeated `Database.migrate()` admits exact v1 schema read-only without rerunning legacy identity purge; unaffected accepted Follow and channel rows remain unchanged.
- Fixed in CP02/CP06: startup now invokes source-aware migration backup before v0 writes; the backup accepts the exact tagged `v0.1.0` schema without `profile_media_assets`, checks referenced media on known main, and publishes protected no-clobber archives.
- Verification blocked: initial full `pytest` cannot import missing `hypothesis`; Node gateway dependency install is unavailable from the local offline npm cache. These are environment blockers, not observed test failures.
- The pre-v1 offline operator gate and source-aware backup were integrated at CP06. Do not deploy an intermediate checkpoint build as a released SQLite v1 until the pinned schema and full integration acceptance have been verified.
- CP05: v1 ORM and provisional physical snapshot contain tenant-scoped canonical AP identities, dual-FK typed remote Discord target claims, local composite reply FK, group-scoped receipts, Follow/relay attempts and immutable owner slots. Unknown historical owner remains NULL; conflicting v0 canonical identities fail admission before writes. Historical matching Lemmy numeric IDs are insufficient provenance because existing `lemmy:` encoded values can be supplied manually; ambiguous accepted rows retain `remote_software_profile='unknown'` until verified discovery. CP24 compares the frozen version-1 schema with fresh bootstrap and both supported v0 upgrade schemas.

- CP06 offline upgrade boundary (2026-11-11): startup creates a verified migration archive under the existing EX lease before any nonempty v0 upgrade, releases EX, then reacquires SH and admits v1 read-only before Discord/HTTP. The external operator must stop/disable all old binaries and restart supervisors; no atomic flock conversion or mixed rolling upgrade is claimed.

- CP07: Bridge #19 autocomplete now matches handle/actor URLs and enforces Discord choice sizes; blocked-origin and empty-result diagnostics use safe reason codes. NodeBB Group discovery was completed at CP08.

- CP08 (2026-11-11): verified NodeBB category/WebFinger/Group discovery, choice revalidation and DNS-pinned read-only HTTP are implemented with fixture tests. Discovery itself does not establish a Follow/Accept or live federation; subsequent checkpoints add the lifecycle and content paths. Full Discord suite remains unverified because its dependencies are missing.

- CP09 (2026-11-11): removed authoritative pre-verification raw-activity ID cache lookups for Announce, Accept and Undo. Trusted nested content now requires Fedify typed serialization; unsupported typed shapes fail closed rather than consuming cached JSON. Gateway `npm test` remains unavailable in this environment because @fedify packages are not installed; the changed TypeScript paths require the gateway suite before release.

- CP10 (2026-11-11): remote Follow now reserves the Group actor/inbox/Follow ID and channel binding atomically before the single CAS-claimed send. Unknown network outcome leaves the original `sending` ID pending; CP11 adds on-demand cancellation and cleanup. Pending NodeBB Groups cannot be activated by implicit content. Signed Accept must originate from the original Group. Gateway `npm test` remains blocked by incomplete node_modules (`@types/node` missing); full Python operation suites require `discord.py`.

- CP11 (2026-11-11): last-channel unsubscribe is now serialized against outbound Follow send. `prepared` is cancelled without network; `sending` retains cancellation pending original delivery; `sent` claims `undo_sending` before deterministic Undo to the saved Group inbox. Before Undo starts, resubscribe reuses the same generation; after Undo may have been sent, unresolved cleanup requires verified remote state and documented guarded offline row deletion. A missing/ambiguous result is not proof of successful unsubscribe. SQLite race/ID scenarios pass; gateway runtime still cannot run without `@types/node`, and Discord command integration remains unverified without `discord.py`.

- CP12 (2026-11-11): remote post/reply fanout reserves canonical Group/object/owner and per-target pre-REST CAS attempts; confirmed delivery commits together with completed claim. Unknown Discord outcomes remain unresolved without blind resend; failed pre-send channel lookup can retry at the next valid Group event. Missing historical Group root cannot be authorized by an arbitrary fetched `audience`/`context`: CP13 adds bounded verified membership lookup for this path. Legacy Lemmy backfill expectations requiring unverified fetch remain open and must be revalidated in CP13, not treated as passing CP12. Focused SQLite/runtime scenario tests passed under temporary test-only shims for missing `discord.py`/`hypothesis`; full Python/Node suites remain unavailable in this environment.

- CP13: verified NodeBB Group Announce URI and nested Note/Article are normalized through a new authenticated internal event; root Notes are posts, replies retain direct parent and verified root. Historical roots require an exact official Group-outbox match within four pages/96 entries; unavailable or older roots beyond this bound remain deferred. NodeBB network and Python/Discord integration are covered by fixtures, but the full gateway Fedify suite remains unavailable without Node dependencies.

- CP14 (2026-11-11): remote Group post/reply mutations now require the persisted original Person owner plus a direct verified owner Update/Delete and matching Group/parent/kind before Discord REST. Group-only announced mutation (including Tombstone), ambiguous tenant/type and historical owner=NULL are denied; moderator delegation remains unsupported until cryptographic proof is available. NodeBB URI Delete resolves root/reply from canonical Group mapping rather than `/post/{pid}`. Focused SQLite tests use test-only SDK import shims; gateway runtime still needs missing npm dependencies for full acceptance.

- CP15–CP16 boundary: remote community Create is durably queued before send; the existing worker now verifies original Group Follow/binding/policy (including zero followers and shared endpoints) and holds ambiguous sends as `unknown` without automatic retry. A locally accepted `queued` result creates canonical Discord source/mirror surfaces and thread-create dedup. The original v0 delivery status CHECK is rebuilt during v1 migration so `unknown` is admissible; populated follower delivery FKs are preserved. Gateway full suite and real Discord SDK integration remain unavailable because packages are missing; CP17 implements original-recipient mutation recovery; full integration tests are still required.

- CP17 (2026-11-12): remote Discord Update/Delete reservations retain immutable direct Group recipients from original Create; `unknown`/unconfirmed Create blocks mutation rendering and later direct revision deliveries remain FIFO. Missing original provenance rejects rather than rebuilding Follow. Focused SQLite/worker scenarios pass; full gateway `npm test` cannot run without Fedify/Node dependencies, and full Python suite remains blocked by `hypothesis`/`discord.py`. Three older migration tests fail identically on unmodified CP16 due to unsupported unversioned test DB admission; not introduced by CP17.

- CP18 (2026-11-12): local-community inbound Follow/Undo generation guard implemented: exact current Follow-ID DELETE, receipt-proven retry against atomic accepted row, superseded/absent retry produces terminal skip without Accept. The 21 focused/neighbor Follow tests pass under a test-only substitute for missing Discord SDK; full HTTP/Fedify/Discord integration remains unverified without project dependencies.

- CP19 (2026-11-12): verified local Group inbox recipients now create separate wire-ID-scoped receipts for actor-specific and bounded shared inbox deliveries, while remote signed Group Announce and Follow/personal receipts retain their existing routing. Python SQLite and pure selector tests pass; gateway Fedify integration and real Discord host placement remain unverified without missing dependencies. CP20–CP21 enforce Service/Person ACL and tenant-specific host reservation; a scoped receipt alone does not authorize Group content.

- CP20 (2026-11-12): local Group inbound Create now verifies the Fedify outer sender, registered Person and exact accepted same-origin Bridge Service metadata and keys before duplicate/host/relay. On actor fetch failure, missing explicit software declaration or revoked Service, Person→Service permission fails closed; older Bridge Service actors without the marker cannot use this new path until upgraded. Existing same-actor subscriber path remains. CP21 adds pre-REST host reservation, and CP23 excludes the original Service from relay. Focused CP20 scenarios use a test-only SDK import shim; full Fedify/Discord integration is unverified without the project dependencies.

- CP21 (2026-11-12): inbound locally hosted Group threads/replies now reserve immutable per-tenant canonical rows and one CAS host attempt before Discord REST. Concurrent differently wrapped Creates cannot create a second host; ambiguous sends stay `unknown` without automatic retries; reply parent resolution and global mapping fallback refuse cross-tenant ambiguity. Verified with SQLite and fake Discord boundary scenarios; full gateway/Discord integration remains unavailable without external packages. Historical `NULL`-owner inbound placement cannot be automatically adopted or repaired from later envelopes and remains fail-closed. CP22/CP23 implement mutation ACL and relay dedup/origin.

- CP22 (2026-11-12): local-hosted Group inbound Update/Delete requires verified direct Person action, accepted immutable origin Service/same-actor follower, original per-tenant owner/parent, and delivered host surface. Explicit signed `inReplyTo` must match normalized ancestry. Group-only Announce mutations and legacy NULL-owner objects remain fail-closed. Focused SQLite/real dispatcher and Discord surface fanout scenarios pass with test-only Discord/Runtime import shims; real gateway/Discord integration still needs missing dependencies. Six older mutation tests were updated because their unsigned, NULL-owner historical fixtures cannot authorize an edit/delete under CP22; new signed owner scenarios cover the permitted path.

- CP23 (2026-11-12): per-local-Group canonical relay Create now locks initial owner/origin Service, immutable first source and optional projection, and per-target stable Announce ID with CAS/fencing before HTTP. Current gateway expressly distinguishes pre-network rejection (`safeToRetry`) from ambiguous send; only the former can retry. Full Fedify/Discord integration remains unverified in this environment (missing dependencies). Historical legacy relay projection without first verified normalized fields is not automatically reconstructed from later Announce, and `sending/unknown` after possible HTTP success requires operator evidence rather than blind retry.

- CP24 release verification (2026-11-12): frozen v1 SQL and the real migration
  runner match for empty, tagged release v0 and supported known-main v0
  schemas, including indexes/triggers; stamped v1 schema drift is rejected and
  repeat admission does not rewrite persisted relay rows. The combined focused
  regression run finished with **185 passed, 1 deselected**; the excluded
  echo-suppression test requires the real activity dispatcher rather than the
  isolated test shim used for SQLite/community verification. Full `pytest`
  stops at `tests/conftest.py` with `ModuleNotFoundError: hypothesis`; the full
  Fedify gateway `npm test` (missing `@fedify/fedify`/Node types) and real
  NodeBB–Discord interoperability **remain unverified**: `discord.py` and
  Node/Fedify dependencies are not installed and offline package caches are
  incomplete. This is an outstanding
  release verification gate, not evidence that those integrations pass. The
  original NodeBB v4.16.3 category-outbox historical-root membership is
  bounded, not a guarantee of full backfill; unproven ancestors are deferred.
  Remote acceptance is not implied by a durable `queued` Create, and external
  HTTP/Discord ambiguous outcomes remain unresolved without blind retries.

- Post-CP24 adversarial verification (2026-11-12): fixed local Group sibling
  content drift: the first verified inbound Create now records immutable
  rendered title/body with canonical host reservation; duplicate Create or
  Announce, safe pre-send retry, and restart reuse this first projection.
  Existing historical host rows without recoverable first-Create content do
  not invent a sibling body from a later event. Full Discord/Fedify acceptance
  remains blocked by unavailable SDK dependencies; isolated host+SQLite
  regression tests are not a substitute for live integration.
- CP24A changes the still-unreleased v1 schema snapshot. A SQLite database
  already stamped `user_version=1` using the earlier CP24 candidate will
  correctly fail strict admission against this revision. Do not apply this
  snapshot amendment in place to such a database; if any CP24 candidate was
  deployed, it requires a separately planned versioned migration instead.

## Post-bundle B6 review (2026-11-13)

- Release gate open: external clean-checkout review reported 1040 passed, 217 failed, 6 errors and 1 xfailed on the feature, versus 988 passed and 6 failed on `main`. Direct NodeBB/Fedify/Discord E2E remains unverified; do not describe CP24 as accepted.
- Corrected in the follow-up review patch: public NodeBB object GET/outbox now read original Create type/context, NodeBB Update JSON preserves the verified conversation root, and a valid same-wire remote Group Create retry can pass a terminal receipt to complete provably unsent Discord forums. Errors before the Discord REST call remain `failed`; unknown network outcomes still block blind resend. The first verified remote Group Create object JSON is now saved with the canonical root/reply before REST: later valid Announce deliveries can complete missing forums but cannot change the original Discord title/body. Previously stored remote groups without this proof fail closed for additional placements.
- Legacy partial-schema tests are not valid v0 inputs to the strict migration runner. The inbound receipt fixture was updated to the actual tagged `v0.1.0` schema; the remaining full suite regressions still need classification on genuine supported v0/v1 fixtures. Node TypeScript/Fedify runtime tests are not executable in this environment without the project dependencies; no gateway runtime pass is asserted.
