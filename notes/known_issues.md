# Known issues

- Simplified startup: only Database.create_all() creates missing metadata tables; retained schema helpers require that initialization and do not repeat it.

- Fixed/verified: startup no longer runs the historical ActivityPub identity rewrite or deletes federation relationships; SQLite versions protect future registered upgrades.
- Test environment baseline: six runtime/OAuth tests fail when the configured SOCKS proxy is used without the optional socksio dependency; unrelated tests are unchanged.
- Known migration boundary: adopting unversioned development databases does not validate historical identities; legacy schema helpers remain outside registered-upgrade atomicity.
- Verified: bridge-local bans do not remove existing remote subscriber rows and do not emit ActivityPub moderation activities.
- Verified: remote handle resolution remains string-only; WebFinger/actor fetch resolution is not implemented.
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
