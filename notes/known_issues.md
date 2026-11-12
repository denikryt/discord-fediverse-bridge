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
- Fixed in CP02, awaiting CP06 startup integration: source-aware migration backup accepts the exact tagged `v0.1.0` schema without `profile_media_assets`, checks referenced media on known main, and publishes protected no-clobber archives.
- Verification blocked: initial full `pytest` cannot import missing `hypothesis`; Node gateway dependency install is unavailable from the local offline npm cache. These are environment blockers, not observed test failures.
- CP05 has an operationally incomplete startup boundary: the v0→v1 runner is now functional and schema-validated, but CP06 must integrate the mandatory verified migration backup under EX deployment lock before production upgrade. Do not deploy CP05 as a release.
- CP05: v1 ORM and provisional physical snapshot contain tenant-scoped canonical AP identities, dual-FK typed remote Discord target claims, local composite reply FK, group-scoped receipts, Follow/relay attempts and immutable owner slots. Unknown historical owner remains NULL; conflicting v0 canonical identities fail admission before writes. Historical matching Lemmy numeric IDs are insufficient provenance because existing `lemmy:` encoded values can be supplied manually; ambiguous accepted rows retain `remote_software_profile='unknown'` until verified discovery. Full version-1 snapshot is provisional until CP24.
