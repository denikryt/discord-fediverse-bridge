# Known issues

- Local-community Discord create retries use missing surface rows as the retry signal. Edit and delete fanout has no durable per-surface mutation receipts, so retries require reprocessing the source event.
- Adopting an unversioned development database does not validate historical ActivityPub identities. Retained legacy schema helpers also remain outside registered-upgrade atomicity.
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
