# Known issues and verified behavior

- Valid inbound activities from communities with no accepted bridge subscription are ACKed at the ActivityPub layer and skipped locally unless they relate to already mapped bridge context.
- Local community Undo(Follow) removes the follower row and future local-community fanout uses current accepted followers as the delivery source of truth.
- `/subscribe-channel` can discover same-instance bridge-owned local communities and persist local subscriber rows. Stage 5 makes active local subscriber forums source-capable for new posts/comments and authoritative for edits/deletes of their own canonical surfaces.
- Stage 2 local-community surface refactor still leaves temporary Stage 1 naming-compatibility for remote subscribers: `src/db.py` keeps `create/get/list/delete/update_local_community_follower...` wrappers and `src/models.py` still exports `LocalCommunityFollower = RemoteSubscriber`. This compatibility exists only so current runtime, fanout, and tests can keep working while the remaining call sites migrate from old `local_community_follower` naming to explicit `RemoteSubscriber` naming. It should be removed once those call sites are fully renamed.
- Local subscriber fanout uses missing surface rows as the create retry signal; edit/delete retries are currently reprocess-based and do not use a dedicated per-surface mutation receipt table.
