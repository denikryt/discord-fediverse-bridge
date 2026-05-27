# Database map

This document explains the SQLAlchemy schema by ownership area, primary writers/readers, and invariants. It owns table-level navigation and persistence concepts; it does not define migrations or runtime algorithms.

Method-level repository split planning lives in `docs/architecture/database-method-inventory.md`. That inventory maps every public `Database` method to its target repository owner, primary call-site areas, relevant tests, and extraction risks.

| Table | SQLAlchemy model | Area | Primary writer | Primary readers | Why it exists | Important invariants |
| --- | --- | --- | --- | --- | --- | --- |
| `post_links` | `PostLink` | Legacy/direct Lemmy mapping | Remote sync runtime | Remote sync runtime | Map Lemmy posts to Discord forum threads | Unique target thread; one row per post/channel copy |
| `comment_links` | `CommentLink` | Legacy/direct Lemmy mapping | Remote sync runtime | Remote sync runtime | Map Lemmy comments to Discord messages | Unique target Discord message; per-thread remote comment dedup |
| `activitypub_event_receipts` | `ActivityPubEventReceipt` | Shared inbound idempotency | `src/activitypub_handlers.py` | Inbound handlers | Prevent duplicate side effects | `delivery_id` preserves idempotency |
| `channel_community_subscriptions` | `ChannelCommunitySubscription` | Remote subscription | Subscribe/unsubscribe operations | Remote sync runtime | Bind Discord forum channel to remote community | One subscription per Discord channel |
| `bridge_actor_follows` | `BridgeActorFollow` | Remote subscription | Subscribe/unsubscribe and lifecycle handlers | Subscribe/unsubscribe, Accept handling | Track one shared bridge actor follow per remote community | One row per remote community actor |
| `users` | `User` | Registration/local identity | Registration service | Publish paths, gateway actor store | Store Discord-owned AP identity | Unique Discord user, username, and actor URL |
| `registration_sessions` | `RegistrationSession` | Registration/local identity | Registration service | HTTP registration routes | Persist browser/OAuth registration state | Unique session token |
| `message_mappings` | `MessageMapping` | Shared object serving/dedup | Content sync runtimes | Edit/delete, dedup | Map source ids to AP activity/object ids | Unique source, activity id, object id, and Discord message id |
| `published_activity_objects` | `PublishedActivityObject` | Shared object serving/dedup | Publish persistence | Gateway object serving | Persist AP objects for later resolution | Unique activity and object ids |
| `community_thread_groups` | `CommunityThreadGroup` | Shared Discord fanout | Remote sync runtime | Fanout/edit/delete paths | One logical thread across Discord channels | Source thread and AP object identify canonical state |
| `community_thread_group_deliveries` | `CommunityThreadGroupDelivery` | Shared Discord fanout | Fanout code | Fanout/edit/delete paths | Per-channel thread copies | Unique thread group/channel and Discord thread |
| `community_message_groups` | `CommunityMessageGroup` | Shared Discord fanout | Remote sync runtime | Fanout/edit/delete paths | One logical message across Discord channels | Parent group preserves replies |
| `community_message_group_deliveries` | `CommunityMessageGroupDelivery` | Shared Discord fanout | Fanout code | Fanout/edit/delete paths | Per-channel message copies | Unique message group/channel and Discord message |
| `remote_actors` | `RemoteActor` | Remote actor cache | Actor cache helpers | Verification/delivery code | Cache inbox and key metadata | Unique actor URL |
| `local_communities` | `LocalCommunity` | Local community hosting | Create community operation | Gateway actor store, local runtime | Discord forum exposed as AP Group | Unique forum channel, slug, and actor URL |
| `remote_subscribers` | `RemoteSubscriber` | Local community hosting | Local follow handler | Local fanout, dashboard | Remote ActivityPub actors following local communities | Unique community/remote actor and Follow id |
| `local_subscribers` | `LocalSubscriber` | Local community hosting | Local subscribe/unsubscribe operations | List/dashboard, later local runtime stages | Same-instance Discord forums subscribed to local communities | One local subscriber role per Discord forum channel |
| `local_community_relay_source_activities` | `LocalCommunityRelaySourceActivity` | Local community hosting | Local relay fanout | Relay retry/delivery code | Persist inbound source activities | Unique community/operation/source object/source activity |
| `local_community_relay_deliveries` | `LocalCommunityRelayDelivery` | Local community hosting | Local relay fanout | Gateway relay result handling | Track per-follower relay attempts | Unique source activity row/target actor |
| `local_community_threads` | `LocalCommunityThread` | Local community hosting | Local runtime | Local reply/edit/delete | Canonical local-community post identity | Unique AP activity and AP object |
| `local_community_thread_surfaces` | `LocalCommunityThreadSurface` | Local community hosting | Local runtime, Stage 2 migration | Local reply/edit/delete | Per-Discord-thread surface for one canonical local-community post | Exactly one host surface; Stage 3 may add local_subscriber surfaces with local_subscriber_id; unique Discord thread and starter message |
| `local_community_messages` | `LocalCommunityMessage` | Local community hosting | Local runtime | Local reply/edit/delete | Canonical local-community comment identity | Unique AP activity and AP object |
| `local_community_message_surfaces` | `LocalCommunityMessageSurface` | Local community hosting | Local runtime, Stage 2 migration | Local reply/edit/delete | Per-Discord-message surface for one canonical local-community comment | Exactly one host surface; Stage 3 may add local_subscriber surfaces with local_subscriber_id; unique Discord message |

`activitypub_event_receipts`, `message_mappings`, `published_activity_objects`, `remote_actors`, and the community group tables are shared infrastructure for deduplication, object lookup, and fanout.
