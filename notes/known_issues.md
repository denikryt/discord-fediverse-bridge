# Known issues

- Verified: bridge-local bans do not remove existing remote subscriber rows and do not emit ActivityPub moderation activities.
- Verified: remote handle resolution remains string-only; WebFinger/actor fetch resolution is not implemented.
- Fixed/verified: Stage 4 startup migration now repairs retained community-sync group AP IDs and same-origin local community actor IDs in `community_thread_groups` and `community_message_groups`.
- Known operational behavior: a globally banned super-admin cannot unban themselves through Discord commands; recovery requires another non-banned super-admin or direct database intervention.

## Local-community relay outcome persistence is not atomic

Gateway outcomes are applied one row at a time through repository methods that open
separate sessions. If persistence fails after one outcome is committed, earlier
rows remain updated while later rows remain pending. Pending rows are retryable,
but the fanout call itself raises and does not return a complete summary.
## Personal-feed history is not replayed after rebinding

Inbound personal-feed comments are retained even when the active binding is removed, moved, or Discord delivery reaches its three-attempt terminal failure. Stage 8 deliberately suppresses synchronization to stale channels and does not reconstruct retained roots, threads, or comments in a replacement Discord channel. Restore/replay tooling remains future work.

- Personal-feed comments tied to an inactive or removed binding remain durable but are not rendered into Discord.
