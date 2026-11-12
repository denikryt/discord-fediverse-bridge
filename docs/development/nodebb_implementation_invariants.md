# NodeBB / Bridge #18/#19 — implementation invariant ledger

This is the pre-implementation acceptance checklist for the single `feature/nodebb-group-federation` branch. The owning CP must add a **positive and negative runtime regression** at the closest trusted boundary; a checked item means both have run and passed. The ledger does **not** equate a fixture shape check or an expected failure (`xfail`) with a passing runtime test. The source of truth for acceptance is the B6 plan (S01–S34), with the 24-CP breakdown determining the patch boundaries.

| Invariant (positive / negative regression) | Owning checkpoints | Verified |
| --- | --- | --- |
| An unmodified v1 SQLite starts read-only with byte-identical subscriptions; v0 is upgraded exactly once / unsupported or mixed sources are rejected without writes. | CP03–CP06, CP24 | [x] CP05 schema + CP06 EX/SH boundary; final E2E at CP24 |
| Release v0 and known-main backups include a restorable database and complete referenced media; separate migration snapshots never clobber / missing media, unrecognized sources or invalid digests stop before upgrade. | CP02, CP06, CP24 | [x] CP02 component; CP06 startup pending |
| Identity cutover changes only owned local identifiers and affected relations / unrelated accepted remote Follow, subscriber and user follower relationships survive restart. | CP04–CP05, CP24 | [ ] |
| Bridge autocomplete finds name/handle/canonical and alternate actor URLs with typed local/remote values; unrelated queries, blocked origins and oversized values do not create a wrong selection. | CP07, CP24 | [x] CP07 direct discovery tests; Discord adapter suite pending dependencies |
| Discord choices resolve public, federated, canonical NodeBB Group identities by verified actor/WebFinger; private/non-Group or blocked/private-network targets cause no HTTP/DB/Follow effects. | CP07–CP08, CP24 | [ ] |
| Each canonical Group has one durable Follow generation shared by channel bindings; explicit signed Accept is checked / late or forged Accept, cancelled Follow and ambiguous Undo never re-enable a subscription. | CP10–CP11, CP24 | [ ] |
| A verified remote Group's signed Announce carries immutable distributor, owner and outer activity identity; forged raw-cache payloads or a foreign Group cannot change verified provenance. | CP09, CP13–CP14, CP24 | [ ] |
| A remote object has one `(Group, object, kind)` canonical owner and per-forum send claim before REST; another Group or an unknown REST outcome never duplicates a known Discord surface. | CP12–CP14, CP24 | [ ] |
| Backfilled historical root is proven in exactly the subscribed Group with bounded authoritative evidence; context/audience-only or foreign root membership does not permit Discord effects. | CP13, CP24 | [ ] |
| NodeBB receives persisted Person-signed Note/Article Create with immutable source and direct Group recipient, including no followers; failed provenance/blocked policy never falls back to Page or sends early. | CP15–CP17, CP24 | [ ] |
| Durable local acceptance (`queued`) creates canonical Discord placement/sibling fanout without asserting remote acceptance; conflict/denial before commit creates none. | CP15–CP16, CP24 | [ ] |
| Incoming local-Group Follow retry cannot overwrite an accepted newer generation or recreate an undone subscriber; a genuinely new first-seen Follow remains possible. | CP18, CP24 | [ ] |
| One signed multi-Group Create is authorized and receipted independently per verified local destination, including shared/actor inbox; unverified destinations and wrong owner are denied before receipt or Discord. | CP19–CP21, CP24 | [ ] |
| Registered Bridge Person inherits publishing rights only from its verified accepted Service for the exact tenant; an unrelated actor, changed key or stale Service cannot write. | CP20–CP22, CP24 | [ ] |
| Local Group host reservation, parent/mutation and mapping remain tenant/owner scoped before Discord REST; duplicate and unknown sends never create a second canonical placement. | CP21–CP22, CP24 | [ ] |
| Relay source and each destination retain original content, owner, origin Service and stable identity; a confirmed target cannot be resent or a failed outcome treated as safe after ambiguous HTTP. | CP23–CP24 | [ ] |
| Existing Lemmy, Bridge, personal-feed and follower-only paths preserve their exact established AP types and routing / NodeBB-specific changes do not silently expand them. | CP07–CP24 | [ ] |

Unresolved external HTTP/Discord outcomes remain `unknown`; there is no promise of exactly-once across a crashed request. No generic receipt reaper, host placement sweeper, second transport, mixed rolling upgrade, or v2 schema is part of this feature.
