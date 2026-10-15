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
