# Known issues

- Local-subscriber fanout still uses missing surface rows as the create retry signal, but edit/delete retries do not yet have a dedicated per-surface mutation receipt table. Mutation retry behavior is therefore still reprocess-based rather than explicitly persisted per target surface.
- Resolved: gateway outbound activity ids now build bridge-owned URLs from `FEDIFY_ORIGIN` with path-aware URL construction. This prevents Undo(Follow) ids like `https://hostactivities/...` when the origin is configured without a trailing slash.
