# Known issues

- Local-subscriber fanout still uses missing surface rows as the create retry signal, but edit/delete retries do not yet have a dedicated per-surface mutation receipt table. Mutation retry behavior is therefore still reprocess-based rather than explicitly persisted per target surface.
