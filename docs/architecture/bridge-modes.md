# Bridge modes

This document explains the boundary between remote Lemmy community subscription mode and local Discord-backed community hosting mode. It owns the conceptual distinction between those modes and points to the implementation files for each one.

## Remote community subscription mode

A Discord forum channel subscribes to a remote Lemmy community. The shared bridge actor follows the remote community. Remote posts and comments delivered to the bridge are fanned out to subscribed Discord channels. Discord posts and comments in subscribed forum threads are published outward as bridge/user ActivityPub content.

Primary files: `src/commands/subscribe.py`, `src/operations/subscribe.py`, `src/commands/unsubscribe.py`, `src/operations/unsubscribe.py`, `src/community_sync/runtime.py`, `src/community_sync/discord_fanout.py`, `src/content_sync/*`, `src/activitypub_handlers.py`, and `fedify-gateway/src/federation-outbound.ts`.

`src/community_sync/` implements remote Lemmy community subscription synchronization. It does not implement Discord-backed local community hosting.

## Local community hosting mode

A Discord forum channel is exposed as a local ActivityPub Group actor. Remote actors can follow the local community. Discord forum posts and comments become ActivityPub content served under local actor/object URLs. Remote inbound ActivityPub content can appear in Discord and can be relayed to remote subscribers when appropriate.

Stage 1 of local-subscriber work adds a second participant type for this mode:

- `RemoteSubscriber` — one remote ActivityPub actor following the local community.
- `LocalSubscriber` — one same-instance Discord forum subscribed to the local community.

Stage 5 makes `LocalSubscriber` forums full create/edit/delete local-community participants. Host forum posts/comments, inbound remote-subscriber posts/comments, and local-subscriber-originated posts/comments all create canonical local-community activity rows and concrete Discord surfaces; edits/deletes from any active participant propagate across the other local Discord surfaces and through the existing ActivityPub update/delete paths.

Primary files: `src/commands/create_community.py`, `src/operations/create_community.py`, `src/local_communities/service.py`, `src/local_communities/runtime.py`, `src/local_communities/federation_fanout.py`, `src/local_communities/activitypub_renderers.py`, `fedify-gateway/src/actor-store.ts`, `fedify-gateway/src/server.ts`, and `fedify-gateway/src/federation-outbound.ts`.

`src/local_communities/` implements Discord forum channels exposed as local ActivityPub Group actors. It does not implement remote Lemmy subscription binding.
