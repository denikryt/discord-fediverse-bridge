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

- Fixed through CP05, pending CP06 startup gate: repeated `Database.migrate()` admits the exact v1 schema read-only instead of rerunning legacy identity purge; unaffected accepted Follow and channel rows remain unchanged.
- Fixed in CP02/CP06: startup now invokes source-aware migration backup before v0 writes; the backup accepts the exact tagged `v0.1.0` schema without `profile_media_assets`, checks referenced media on known main, and publishes protected no-clobber archives.
- Verification blocked: initial full `pytest` cannot import missing `hypothesis`; Node gateway dependency install is unavailable from the local offline npm cache. These are environment blockers, not observed test failures.
- CP05 has an operationally incomplete startup boundary: the v0→v1 runner is now functional and schema-validated, but CP06 must integrate the mandatory verified migration backup under EX deployment lock before production upgrade. Do not deploy CP05 as a release.
- CP05: v1 ORM and provisional physical snapshot contain tenant-scoped canonical AP identities, dual-FK typed remote Discord target claims, local composite reply FK, group-scoped receipts, Follow/relay attempts and immutable owner slots. Unknown historical owner remains NULL; conflicting v0 canonical identities fail admission before writes. Historical matching Lemmy numeric IDs are insufficient provenance because existing `lemmy:` encoded values can be supplied manually; ambiguous accepted rows retain `remote_software_profile='unknown'` until verified discovery. Full version-1 snapshot is provisional until CP24.

- CP06 offline upgrade boundary (2026-11-11): startup creates a verified migration archive under the existing EX lease before any nonempty v0 upgrade, releases EX, then reacquires SH and admits v1 read-only before Discord/HTTP. The external operator must stop/disable all old binaries and restart supervisors; no atomic flock conversion or mixed rolling upgrade is claimed.

- CP07: Bridge #19 autocomplete now matches handle/actor URLs and enforces Discord choice sizes; blocked-origin and empty-result diagnostics use safe reason codes. NodeBB Group discovery is still pending CP08.

- CP08 (2026-11-11): verified NodeBB category/WebFinger/Group discovery, choice revalidation and DNS-pinned read-only HTTP are implemented with fixture tests. Remote Follow cancellation, profile persistence and NodeBB content ingest/publish remain future checkpoints; do not treat discovery as complete NodeBB federation. Full Discord suite remains unverified because its dependencies are missing.

- CP09 (2026-11-11): removed authoritative pre-verification raw-activity ID cache lookups for Announce, Accept and Undo. Trusted nested content now requires Fedify typed serialization; unsupported typed shapes fail closed rather than consuming cached JSON. Gateway `npm test` remains unavailable in this environment because @fedify packages are not installed; the changed TypeScript paths require the gateway suite before release.

- CP10 (2026-11-11): remote Follow now reserves the Group actor/inbox/Follow ID and channel binding atomically before the single CAS-claimed send. Unknown network outcome leaves the original `sending` ID pending; CP11 must implement safe on-demand cancellation and cleanup. Pending NodeBB Groups cannot be activated by implicit content. Signed Accept must originate from the original Group. Gateway `npm test` remains blocked by incomplete node_modules (`@types/node` missing); full Python operation suites require `discord.py`.

- CP11 (2026-11-11): last-channel unsubscribe is now serialized against outbound Follow send. `prepared` is cancelled without network; `sending` retains cancellation pending original delivery; `sent` claims `undo_sending` before deterministic Undo to the saved Group inbox. Before Undo starts, resubscribe reuses the same generation; after Undo may have been sent, unresolved cleanup requires verified remote state and documented guarded offline row deletion. A missing/ambiguous result is not proof of successful unsubscribe. SQLite race/ID scenarios pass; gateway runtime still cannot run without `@types/node`, and Discord command integration remains unverified without `discord.py`.

- CP12 (2026-11-11): remote post/reply fanout reserves canonical Group/object/owner and per-target pre-REST CAS attempts; confirmed delivery commits together with completed claim. Unknown Discord outcomes remain unresolved without blind resend; failed pre-send channel lookup can retry at the next valid Group event. Missing historical Group root cannot be authorized by an arbitrary fetched `audience`/`context`: CP12 defers this path until CP13 implements bounded membership verification. Legacy Lemmy backfill expectations requiring unverified fetch remain open and must be revalidated in CP13, not treated as passing CP12. Focused SQLite/runtime scenario tests passed under temporary test-only shims for missing `discord.py`/`hypothesis`; full Python/Node suites remain unavailable in this environment.

- CP13: verified NodeBB Group Announce URI and nested Note/Article are normalized through a new authenticated internal event; root Notes are posts, replies retain direct parent and verified root. Historical roots require an exact official Group-outbox match within four pages/96 entries; unavailable or older roots beyond this bound remain deferred. NodeBB network and Python/Discord integration are covered by fixtures, but the full gateway Fedify suite remains unavailable without Node dependencies.

- CP14 (2026-11-11): remote Group post/reply mutations now require the persisted original Person owner plus a direct verified owner Update/Delete and matching Group/parent/kind before Discord REST. Group-only announced mutation (including Tombstone), ambiguous tenant/type and historical owner=NULL are denied; moderator delegation remains unsupported until cryptographic proof is available. NodeBB URI Delete resolves root/reply from canonical Group mapping rather than `/post/{pid}`. Focused SQLite tests use test-only SDK import shims; gateway runtime still needs missing npm dependencies for full acceptance.
