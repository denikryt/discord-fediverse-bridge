# Gateway/Python contract

This document explains the internal API contract between the Fedify gateway and the Python bridge in both directions. It owns payload categories, authentication expectations, and route-level side effects; exact validation remains in source types.

## Gateway -> Python

The gateway uses one authenticated internal bridge API. `POST /internal/activitypub/events` delivers normalized activities; `/internal/fedify/*` supplies actor identities, signing keys, subscribers, published objects, mappings, and operator subscription rows. Authentication uses the `FEDIFY_SHARED_SECRET` bearer token. The Python bridge is the only database owner.

Common fields: `event_type`, `delivery_id`, `occurred_at`, `actor_id`, `community_actor_id`, and `object` or event-specific object payload.

Supported normalized event categories: `post.created`, `post.updated`, `post.deleted`, `comment.created`, `comment.updated`, `comment.deleted`, `follow.accepted`, `local.follow_requested`, `local.unfollow_requested`, `remote.group_announced`.

For `remote.group_announced`, `actor_id` and `community_actor_id` identify
**the Fedify-verified Group distributor**, not the claimed Person author.
`delivery_id` is the outer signed `Announce.id`; `object.ap_id` is its URI or
verified inline Note/Article ID. `source_activity_json` preserves the verified
outer envelope, with an optional inner Create ID carried separately. Python
requires the persisted accepted `nodebb` Follow, actor/inbox equality and
policy before fetching URI objects with the bounded DNS-on-connect adapter.
The fetched `Note.attributedTo` becomes the immutable canonical owner; it is
not represented as an independent Person signature. A historical reply root
requires exact membership in that Group's bounded official outbox.

### Inbound remote mutation authorization

For `post.updated|deleted` and `comment.updated|deleted`, the gateway sends
`source_activity_json` from the Fedify-verified **direct** Update/Delete and
retains `source_announce_id` for Group-wrapped deliveries. Python requires the
canonical persisted `(community_actor_id, ap_object_id, kind, source_owner_actor_id)`
from Create and exact signed actor/object/Group identity before any Discord edit.
A Group-signed `Announce(Update|Delete)`, even with a claimed matching inner
`actor`, does **not** prove the Person performed the mutation: until separately
verified owner action or explicitly proven moderator authority is supported,
this path returns `ignored_unauthorized_mutation`. `owner=NULL` historical
rows fail closed. Before receipt admission, a Delete URL is classified only
against the saved Group-scoped canonical post/reply, not the URI suffix;
`delivery_id` and the external header remain the original wire values.

## Python -> Gateway

`src/fedify_gateway_client.py` calls authenticated internal gateway routes. Python decides bridge policy and persistence; the gateway owns actor documents, object URLs, HTTP signatures, and signed federation delivery.

| Route | Python caller method | Purpose | Important payload fields | Result or side effect |
| --- | --- | --- | --- | --- |
| `/prepare-community-follow` | `prepare_community_follow` | Verify canonical Group and allocate Follow identity without sending | `communityActorUrl` | Returns verified `communityActorUrl`, `communityInboxUrl`, and immutable `followActivityId` |
| `/send-prepared-community-follow` | `send_prepared_community_follow` | Submit only an already committed Follow | `communityActorUrl`, `communityInboxUrl`, `followActivityId` | Sends exactly the reserved Follow; timeout outcome remains unresolved in Python |
| `/follow-community` | `follow_community` | Legacy manual gateway send (not used by subscription operation) | `communityActorUrl` | Returns community actor URL, inbox URL, and Follow activity id |
| `/unfollow-community` | `unfollow_community` | Send Undo(Follow) to original saved Group inbox | `communityActorUrl`, `followActivityId`, optional `communityInboxUrl` from verified persisted Follow | Returns `ok` only after gateway send succeeds; Python claims `undo_sending` before calling and keeps ambiguous outcome unresolved |
| `/render-community-create` | `render_community_create` | Render immutable community Create without remote fetch, signing or network delivery | canonical User Actor IDs, SHA-256 `operationId`, persisted `softwareProfile`, Group actor, parent/root, timestamp | Returns exact `activityId`, `objectId`, `publishedAt`, and JSON; Python checks all identities/addressing before DB commit |
| `/publish` | `publish_content` | Legacy gateway send route (remote Discord runtime no longer calls it) | `actorUsername`, `communityActorUrl`, `kind`, `bodyMarkdown` | Sends immediately; not suitable for persist-before-send community publishing |
| `/publish-local-community` | `publish_local_community_content` | Publish local-community content | publish fields | Returns ids and delivery counts |
| `/send-local-community-relay` | `send_local_community_relay` | Deliver rendered relay activities | `signingActorUrl`, `deliveries[]` | Returns per-target outcomes |
| `/accept-local-community-follow` | `accept_local_community_follow` | Send Accept(Follow) | community, remote actor, inbox, Follow id | Sends signed Accept |
| `/update` | `update_content` | Send Update | actor, community, object, kind, body | Sends Update |
| `/delete` | `delete_content` | Send Delete | actor, community, object | Sends Delete |

### Durable remote community Create (CP15/CP16 boundary)

The Discord remote-community service derives one operation digest from the immutable
`(guild_id, forum_channel_id, Discord message_id, registered Person, canonical Group, kind)`.
Typed User Actor object/Create IDs and the original UTC Discord timestamp are sent
to `/render-community-create`. The gateway returns **only JSON**, without querying
remote actors or sending ActivityPub. It renders Note roots for persisted `nodebb`,
Page roots for confirmed `threadiverse`/`bridge`, and Note replies for each.
A NodeBB root uses its own canonical object ID as `context`; replies carry the
verified conversation root and immediate `inReplyTo`, not a guessed remote topic URL.
`unknown` software profile is rejected before rendering.

Python checks the returned Create/object IDs, author, Group addressing, parent,
source, kind and timestamp. Under a single SQLite writer transaction it checks
accepted Follow/binding generation again, then persists `MessageMapping`,
`PublishedActivityObject`, one ready `UserActorOutboundSource`, immutable
`snapshot_json.directGroupRecipients[]` (Group actor, inbox, Follow ID, Discord
forum/guild, software profile), follower snapshots and deduplicated endpoints.
The direct Group recipient is not a synthetic follower. An exact duplicate
reuses the original source without rendering or changing its JSON.

`PublishResult(status="queued",locally_accepted=True,delivery_state="queued")`
means only **local durable admission**. The community runtime persists canonical
thread/reply placements and sibling Discord deliveries on this result without
waiting for the remote Group. The bot deduplicates repeated thread-create events.
The existing User Actor delivery worker authorizes a direct Group route from the
original source snapshot, checking the currently accepted **same** Follow ID,
Group actor, inbox, forum binding and federation policy under its claim
boundary. A matching follower endpoint shares one send, but a follower grant
cannot bypass revoked Group authorization. Ambiguous direct sends persist as
`UserActorOutboundDelivery.status="unknown"` and are not automatically retried;
this is not evidence of NodeBB acceptance. Follower-only retry is unchanged.
The old local-community `/publish-local-community` path is independent.

## Bridge signing key ownership

Python initializes the bridge actor key in `bridge_actor_keys` before readiness. The gateway reads that row from the shared SQLite database; it does not generate an in-memory bridge identity or require bridge JWK environment variables during normal runtime.

## Verified inbound envelope boundary (NodeBB/Bridge checkpoint CP09)

Fedify signature verification precedes content extraction. The gateway uses
`Announce.toJsonLd()` only from the verified callback, matching its typed
`id`, `actorId`, and `objectId`; it never replaces those values with a raw
request cached under an activity ID. If an embedded Create/Update/Delete cannot
be recovered from verified typed serialization, it is dropped before the Python
events endpoint rather than trusting an unverified copy. This remains a
functional limit until the NodeBB URI/Note normalization checkpoint.

For nested `Announce(Create|Update|Delete)`, `delivery_id` and the
`X-Bridge-Delivery-Id` internal header identify the **outer** Announce;
`source_activity_id` identifies the nested activity. `Accept` obtains its
original Follow ID from verified `Accept.objectId`, while `Undo(Follow)`
requires a complete typed embedded Follow with the same verified sender and
an actor-specific inbox owner matching the target when that route is used.
Request-captured raw JSON is diagnostic only and cannot create or delete a
remote-subscription relationship. A signed announcement from an accepted
remote Group can still arrive in the shared Service inbox without a locally
hosted Group destination; the local-host recipient rules belong to a separate
verified ingress path.
