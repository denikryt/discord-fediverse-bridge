# Bridge modes

This document explains the boundary between remote Lemmy community subscription mode and local Discord-backed community hosting mode. It owns the conceptual distinction between those modes and points to the implementation files for each one.

## Remote community subscription mode

A Discord forum channel subscribes to a remote Lemmy community. The shared bridge actor follows the remote community. Remote posts and comments delivered to the bridge are fanned out to subscribed Discord channels. Discord posts and comments in subscribed forum threads are published outward as bridge/user ActivityPub content.

Primary files: `src/commands/subscribe.py`, `src/operations/subscribe.py`, `src/commands/unsubscribe.py`, `src/operations/unsubscribe.py`, `src/community_sync/runtime.py`, `src/community_sync/discord_fanout.py`, `src/content_sync/*`, `src/activitypub_handlers.py`, and `fedify-gateway/src/federation-outbound.ts`.

`src/community_sync/` implements remote Lemmy community subscription synchronization. It does not implement Discord-backed local community hosting.

## Local community hosting mode

A Discord forum channel is exposed as a local ActivityPub Group actor. Remote actors can follow the local community. Discord forum posts and comments become ActivityPub content served under local actor/object URLs. Remote inbound ActivityPub content can appear in Discord and can be relayed to remote subscribers when appropriate.

initial persistence implementation of local-subscriber work adds a second participant type for this mode:

- `RemoteSubscriber` — one remote ActivityPub actor following the local community.
- `LocalSubscriber` — one same-instance Discord forum subscribed to the local community.

participant mutation support makes `LocalSubscriber` forums full create/edit/delete local-community participants. Host forum posts/comments, inbound remote-subscriber posts/comments, and local-subscriber-originated posts/comments all create canonical local-community activity rows and concrete Discord surfaces; edits/deletes from any active participant propagate across the other local Discord surfaces and through the existing ActivityPub update/delete paths.

Primary files: `src/commands/create_community.py`, `src/operations/create_community.py`, `src/local_communities/service.py`, `src/local_communities/runtime.py`, `src/local_communities/discord_create.py`, `src/local_communities/inbound_create.py`, `src/local_communities/mutations.py`, `src/local_communities/followers.py`, `src/local_communities/discord_surface_fanout.py`, `src/local_communities/discord_fanout.py`, `src/local_communities/federation_fanout.py`, `src/local_communities/activitypub_renderers.py`, `fedify-gateway/src/actor-store.ts`, `fedify-gateway/src/server.ts`, and `fedify-gateway/src/federation-outbound.ts`.

`src/local_communities/` implements Discord forum channels exposed as local ActivityPub Group actors. It does not implement remote Lemmy subscription binding.

## Personal-feed control-plane mode

A registered local User Actor may own one Discord guild channel assignment for a future personal federated feed. personal feed control plane stores only the assignment and classifies eligible root-message events; it does not publish ActivityPub content.

The binding is a separate bridge mode, not a remote subscription, local community, local subscriber, or token-linked channel. Database uniqueness enforces one binding per user, guild, and channel. Bind and move operations require the invoking registered user to have Discord Manage Server permission in the target guild, require an ordinary protocol `GUILD_TEXT` channel, and reject channels already owned by another supported bridge mode. Unbind and move preserve historical publication and message mappings for later work.

`src/personal_feeds.py` owns binding mutations and active-binding lookup. `src/discord_event_router.py` exposes classification and current-channel comparison without publishing, replying, editing, deleting, or creating threads.

**local subscriber synchronization Local Community inbound transport:** When `LocalCommunityRuntime` is
bound to the production `DiscordAdapter`, an authorized remote ActivityPub
Create creates the host forum thread or host reply through `PlatformDeliveryPort`
and uses the same typed fan-out for active local subscriber forums. Source
provenance and the remote actor's accepted Follow are checked before duplicate
recovery; a current host placement and guild policy are checked before each
host create. Host receipt validation preserves the separate Discord thread and
starter IDs, and nested replies resolve the actual parent receipt per forum.
The runtime checks host ownership again after outbound I/O and suppresses
subsequent fan-out/relay if the origin Follow was revoked in flight. Old
SDK-based direct-call branches remain for existing tests pending local subscriber synchronization
legacy entry-point removal. This change does not implement local subscriber participation durable
recovery for unknown Discord create results.
