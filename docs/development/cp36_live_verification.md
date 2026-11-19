# Plan 146 CP47 — live verification and evidence protocol

**Candidate:** CP46 release candidate `55ce0dd` (the later CP47 commit changes verification documentation only). **Status:** **NOT VERIFIED**. This checkout has no controlled Discord/NodeBB instance, signed peer inbox, authenticated bot token, live SQLite database or authorization to deploy or alter peer data. No live test has been performed. A local mocked transport, successful ingress HTTP status or a SQLite receipt is **not** evidence of a live Discord POST/GET or remote application.

## Authorization and safe setup

1. Obtain explicit owner approval for the exact candidate deployment, affected development guilds/channels, identities, external peers, test messages and timeout/failure injection. Do not edit roles, webhook credentials, existing Follow relationships or peer source without separate approval. Keep the original Discord source/starter delete behavior untouched.
2. Stop all writers and supervisors before DB upgrade; back up the entire DB, `.git`/deployment configuration and required media, preserve ownership/permissions, record hashes and verify restore. `docs/DEPLOY.md` specifies the accepted v2→v3 path. Never run a CP35 binary against v3. No automatic repair of old v2 or ambiguous claims.
3. After approved migration, check the *actual* operational DB via a read-only connection: `PRAGMA user_version = 3`, `PRAGMA integrity_check = ok`, `PRAGMA foreign_key_check` empty, exact `v3.sql` schema fingerprint and no second migration archive after a second startup. Record counts and original IDs of all Group mappings, proven deliveries, `unknown`/`claimed` rows and receipts before and after. Do **not** upload raw DB, PEM, token, webhook URL or DM text to shareable evidence.
4. Record actual Git SHA, Gateway SHA, Python release version, deployed process IDs, timezone, relevant NodeBB and bridge versions, bot intent/permissions, subscribed accepted Group ID(s), registered Discord U/V owners, tested guild/channel IDs and the exact signed actor identities. Peer limitations must be separated from bridge defects.
5. Capture an immutable per-scenario evidence directory with redacted signed activity (outer `Announce` signature-verification verdict, Group/Person Actor IDs, Activity ID, `object.id`, original `inReplyTo`, addressed recipients), Python ingress receipt, SQLite row/revision/fingerprint/claim state, worker attempt ID, Discord GET/actual thread, and peer-side post. Save original ingress bytes privately when permitted, not just normalized JSON.

## Database observation (read-only)

Use SQLite URI `mode=ro` or the owner's approved read-only snapshot, not an application's writer connection. The following are schema-relative examples; use the actual IDs, never fill them with guessed values:

```sql
SELECT id, community_actor_id, ap_object_id, source_owner_actor_id,
       view_revision, view_state, last_view_activity_id
  FROM community_thread_groups WHERE ap_object_id = :object_id;
SELECT id, community_actor_id, ap_object_id, source_owner_actor_id,
       view_revision, view_state, last_view_activity_id
  FROM community_message_groups WHERE ap_object_id = :object_id;
SELECT id, kind, status, discord_channel_id, thread_group_id, message_group_id,
       reserved_view_revision, applied_revision, reconciliation_marker_version,
       mutation_attempt_token, next_attempt_at, last_error
  FROM community_discord_target_claims WHERE id = :claim_id;
```

Cross-check Group and object IDs rather than relying on `/post/{number}` URL shape. A `processed` receipt only establishes durable projection commit; target success additionally requires a proven Discord message ID, claimed/applied revision and a fresh Discord GET with exact content and ownership.

## Live scenario matrix

Each ID starts **NOT VERIFIED**. Record UTC action time, exact input, signed sender evidence, expected vs actual SQLite state, Discord GET and remote post, plus latency/window. Only mark PASS when *all* required external effects and negative controls have been observed.

| ID | Action and observable result | Result |
| --- | --- | --- |
| G01 / R01 | On an accepted NodeBB Group, delete an already-mapped comment (`/post/79` class); require `comment.deleted`, exact Group+Note mapping, no `post.deleted` fallback, original Discord comment updated/removed according to this bridge's retained-view behavior. Repeat Activity ID: no second effect. Wrong Group/owner/missing mapping: zero side effect. | NOT VERIFIED |
| G02 / R02 | Send genuinely signed `Announce(Update(Note))` from subscribed Group G for existing root, then reply; compare authoritative *outer* Group and immutable inner owner/parent, new Group-view revision, actual Discord edits of all retained copies, no impersonated Person Update. Hostile `Announce` from H or changed `attributedTo` denied. | NOT VERIFIED |
| G03 / R02 | Send genuine Group `Announce(Delete)` after Create; tombstone only G's distribution. Add destination B after Delete while original Person Note GET is still 200; B receives **no new POST**. Another subscribed Group H's same Note remains independent. | NOT VERIFIED |
| G04 / R02 | U1→U2→identical U1 replay: same receipt/revision, latest content unchanged. Same signed Activity ID with changed body/owner/Group: explicit conflict and no revision. A failed U1 retry after a later Delete cannot resurrect a Discord copy. | NOT VERIFIED |
| G05 / R02 | Authorized controlled *lost acknowledgement* of Community root and comment Create where Discord actually accepted the POST: claim `unknown` with no delivery ID, later Group Delete, exactly one matching bot-owned history message adopted; applied current revision, no duplicate POST, including restart. Do not inject loss without explicit approval. | NOT VERIFIED |
| G06 / R02 | Wrong bot/webhook, wrong thread/forum, object URL substring, quoted marker, `/42` versus `/420`, two matches, inaccessible/partial history, legacy markerless `unknown` and stale `claimed`: no false adoption or blind retry. Persist diagnostics for manual review. | NOT VERIFIED |
| G07 / R02 | In-flight Create acknowledged after a Group Update/Delete: preserve reserved old revision and converge to latest; process restart before/after terminal receipt transaction demonstrates no orphaned `in_progress`, no second revision, no second POST. | NOT VERIFIED |
| D01 / R03 | Edit the actual user-authored source Community starter (normal, empty string, edited again); canonical AP object ID, owner, title and recipients unchanged, one durable Update, intended Discord mirror IDs updated. Absent raw `content` triggers fetch; editing a bot mirror/unmapped starter/denied guild does not publish. Do **not** delete the thread or source message for this scenario. | NOT VERIFIED |
| P01 / R05 | Create title-less Personal Note, edit Cyrillic/emoji body, fetch public ActivityPub Create/GET/Update and read receiver's resulting Note: no synthetic `name`; correct Actor, object ID, `inReplyTo` where applicable, and no extra Create. Confirm explicitly titled Community post still has its existing title semantics. | NOT VERIFIED |
| P02 / R06 | Real webhook root Create→Edit→Edit: webhook account shows author, body does not duplicate author heading; full canonical `object_id` is the last line and matches source. Bot comment/mirror retains exactly one header marker; original Discord user messages stay native. | NOT VERIFIED |
| P03 / R07 | In each of two guilds, owner U follows Actor `@bob@example` then assigns/removes/undoes via exact URL and verified handle; same durable Follow generation, same-guild conflict refused, owner V unaffected, offline exact-URL cancellation works. Denied host, mismatched resolver actor and lost permissions do not mutate another owner's data. | NOT VERIFIED |
| P04 / R08 | NodeBB Person `/uid/2` Follow with verified `preferredUsername=nachitima`: at most one owner DM shows `@nachitima@space.nachitima.com`; no profile/mismatch fallback remains safe. Signed Accept and durable Follow ID unchanged. | NOT VERIFIED |
| P05 / R09 | Actual saved Discord `message.content` and opened URL for root/comment at 1999/2000/2001 UTF-16 units, emoji, two links, CW/media, nested `(...)` URL and escaped `\]` label; fitting crossing links remain whole/clickable, mandatory footer and marker survive. Unfit URL omitted whole. Verify client rendering, not only CommonMark syntax. | NOT VERIFIED |
| F01 / regression | CP20 F04/F08/F10/F12/F25/F26 positive controls: NodeBB Create, one canonical reply plus cross-guild mirror, edit latest revision, thread-local parent, Personal Feed round trip. Verify Discord IDs and NodeBB rows, not just HTTP status. | NOT VERIFIED |
| F02 / regressions | Pending-parent after 80 blocked; uncertain-root after 20; Delete after 40 failing mutations; Follow key recovery with two generations; race between competing assignments; cancellation of last owner suppresses not-yet-posted work. Run only in controlled disposable feed/guild, retain actual producer and worker evidence. | NOT VERIFIED |
| X01 / peer boundaries | NodeBB cached remote-parent `toPid` bug (compare original signed `inReplyTo` and NodeBB row); older sandbox timezone-less Update and unknown comment Delete grammar; external input `[label]()` introduced upstream. Record as peer limitations, not bridge PASS or a reason to rewrite canonical objects. | NOT VERIFIED |

## Evidence schema

For every scenario store `{scenario_id, timestamp_utc, candidate_sha, gateway_sha, input_actor_id, outer_verified_actor_id, activity_id, group_id, object_id, canonical_revision_before, canonical_revision_after, claim_id, claim_status_before, claim_status_after, applied_revision, discord_message_id, discord_get_status, actual_discord_content_hash, peer_post_id, peer_delivery_observed, verdict, reason, evidence_files}`. Never fabricate IDs, hashes or timestamps. `NOT VERIFIED` is mandatory for unexecuted steps, unavailable second actor, missing Lemmy/Mastodon/Person T, non-reproducible races or partial history.

## Cleanup and release decision

Document test-owned channels, permissions, webhook identity and Follow relationships before cleanup. Restore only authorized temporary settings, verify final remote Undo and absence of new delivery with real receiver data, leave unrelated content untouched, and retain rollback files privately. Recheck original data/roles and DB integrity after cleanup. A successful local test, an ingress receipt or a peer HTTP 202 is insufficient to mark field acceptance PASS.

**Current decision:** **BLOCKED / NOT VERIFIED for live acceptance**. No deployment or live database migration occurred as part of CP47. The release remains unapproved until the relevant signed-peer and Discord tests are observed and the blocked full suites are rerun on an environment with their actual locked dependencies.
