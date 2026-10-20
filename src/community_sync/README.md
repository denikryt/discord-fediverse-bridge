# community_sync

## Purpose

This package implements remote Lemmy community subscription synchronization, not local community hosting.

## Responsibility

Remote subscribed-channel runtime behavior, inbound Discord fanout, and remote subscription mapping.

## Not responsible for

Local community actor hosting, local follower state, or gateway route ownership.

## Primary entry points

`runtime.py`, `discord_fanout.py`, `delivery_mapping.py`, `inbound_mapping.py`, `reply_mapping.py`, `edit_delete.py`.

## Important tables or payloads

`channel_community_subscriptions`, `bridge_actor_follows`, community group tables, and normalized post/comment events.

## User moderation boundary

Discord-originated remote-subscription publishing resolves the registered local identity before side effects. Active global bans reject the source action and reply only in the source thread. Discord-to-Discord mirror attribution uses the registered `user@bridge-authority` handle rather than guild display names.

## Stage 03 Gateway forum ingress (Checkpoint 32)

Production Discord forum `ThreadCreated` and `MessageCreated` are normalized
by `DiscordAdapter` and routed by the single `PlatformEventIngress`. The
`DiscordEventRouter` checks the source forum before invoking
`CommunityRuntime.handle_normalized_thread_create()` or
`handle_normalized_message_create()`. Physical sibling deliveries use the
connector-scoped `PlatformDeliveryPort`, not an additional SDK callback path.
A starter must have a verified physical `MessageRef`; an unavailable starter
does not cause the router to invent one from arbitrary thread history. SQLite
thread/message groups remain the durable deduplication boundary. Concurrent
`THREAD_CREATE` callbacks for one connector/thread are serialized through
the AP publish and SQLite receipt boundary; a failed first callback does not
poison a process-local seen cache.

## Stage 03 inbound ActivityPub mutations (Checkpoint 36)

Remote Community `post.updated` / `post.deleted` and `comment.updated` /
`comment.deleted` now edit the persisted per-destination Discord copies through
`Runtime.discord_adapter` and connector-scoped `MessageRef` identities. Post
edits replace the starter body; comment edits first fetch the exact physical
message to preserve its attribution header. Deletes continue to write the
visible `*deleted by creator*` tombstone rather than physically deleting a
thread or message. User-authored source comments remain excluded from remote
comment mutation fan-out. One failed physical edit does not block other copies.
Legacy direct-runtime test entry points continue to use the SDK helper when no
adapter is registered; production chooses only the adapter path.

## Stage 03 Checkpoint 41 — guarded parent-post dereference

The shared `bridge_lemmy_to_discord._fetch_ap_object()` now delegates to
`activitypub_safe_fetch.fetch_public_ap_object()`. Before backfill can
reconstruct an unknown parent Page/Article, the HTTPS target must be
credential-free, its A/AAAA set public-only, and its actual socket destination
rechecked. Redirects and implicit proxies are disabled; TLS keeps the original
hostname and JSON bodies have a 1 MiB bound. Canonical ID/type validation
remains in `backfill_normalized_post_as_thread_group()`. This is not an Actor
author-authentication mechanism. Other SDK-shaped inbound operations remain
subject to the final Stage 03 dependency audit.
