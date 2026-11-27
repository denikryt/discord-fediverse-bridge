# Known issues

- Six scenarios in `tests/behavior/test_local_community_remote_fanout_scenarios.py` fail on the unmodified branch and require reconciliation with the current relay and mutation contracts.
- `/subscribe-community` can complete a subscription but fail to respond when the Discord interaction expires during the operation.
- Local-subscriber fanout still uses missing surface rows as the create retry signal, but edit/delete retries do not yet have a dedicated per-surface mutation receipt table. Mutation retry behavior is therefore still reprocess-based rather than explicitly persisted per target surface.

- Removing a bot-created Incoming webhook prevents the bridge from editing or deleting root posts originally sent by that webhook. The worker records the per-destination error and does not replace the author of the retained post.

- Plan 146 Group-view and Person Feed repairs still require live re-verification with signed NodeBB and Discord API; local SQLite/transport tests do not establish peer delivery or clickable Markdown rendering.
- NodeBB 4.16.7 may lose nested `toPid` when the parent is cached as a remote object; the bridge's retained `inReplyTo` and native Discord parent must not be silently rewritten to compensate.
- Discord Community `THREAD_DELETE` remains outside the implemented handlers. Plan 146 deliberately leaves source `MESSAGE_DELETE` and outgoing author signing semantics untouched.
- An older sandbox bridge may produce timezone-free Update timestamps and fail to classify current comment Delete IDs; bridge canonical ActivityPub IDs are not rewritten for peer compatibility.
