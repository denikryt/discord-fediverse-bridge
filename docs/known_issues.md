# Known issues

- Six scenarios in `tests/behavior/test_local_community_remote_fanout_scenarios.py` fail on the unmodified branch and require reconciliation with the current relay and mutation contracts.
- `/subscribe-community` can complete a subscription but fail to respond when the Discord interaction expires during the operation.
- Local-subscriber fanout still uses missing surface rows as the create retry signal, but edit/delete retries do not yet have a dedicated per-surface mutation receipt table. Mutation retry behavior is therefore still reprocess-based rather than explicitly persisted per target surface.

- Removing a bot-created Incoming webhook prevents the bridge from editing or deleting root posts originally sent by that webhook. The worker records the per-destination error and does not replace the author of the retained post.
