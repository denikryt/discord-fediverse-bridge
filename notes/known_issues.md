# Known issues

- Stage 01 review fixes: typed claims reject incompatible channel kinds before
  owner FK updates; `create_all()` remains schema-only and app fixtures use
  `migrate()`. Mixed Accept, local fanout, dashboard, listing and Personal Feed
  workflows have runtime regressions. Personal Feed channel admission also
  checks the requested user, preventing cross-user channel collisions.

- Stage 01 uses registered atomic v1→v2 migration with FK suspension before
  the transaction and restoration after commit/rollback. Generated roles,
  scoped copies, retry CAS and cascade preservation are covered by regressions.
- Gateway Follow dispatch occurs before local persistence. Concurrent external
  dispatches cannot be made exactly-once by the database claim transaction;
  persistence rechecks ownership and fails closed on conflicting changes.

- Local-community Discord create retries use missing surface rows as the retry signal. Edit and delete fanout has no durable per-surface mutation receipts, so retries require reprocessing the source event.
- Nonempty unversioned development databases are rejected at startup; they require an explicitly prepared versioned baseline. Retained legacy helpers are not part of current startup.
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
