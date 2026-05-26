# Known issues and verified behavior

- Valid inbound activities from communities with no accepted bridge subscription are ACKed at the ActivityPub layer and skipped locally unless they relate to already mapped bridge context.
- Local community Undo(Follow) removes the follower row and future local-community fanout uses current accepted followers as the delivery source of truth.
- `/subscribe-channel` can now discover same-instance bridge-owned local communities, but selecting one still returns a temporary "not implemented yet" message because local Discord channel subscription persistence has not landed yet.
- Stage 2 local-community surface refactor still leaves temporary Stage 1 naming-compatibility for remote subscribers: `src/db.py` keeps `create/get/list/delete/update_local_community_follower...` wrappers and `src/models.py` still exports `LocalCommunityFollower = RemoteSubscriber`. This compatibility exists only so current runtime, fanout, and tests can keep working while the remaining call sites migrate from old `local_community_follower` naming to explicit `RemoteSubscriber` naming. It should be removed once those call sites are fully renamed.
