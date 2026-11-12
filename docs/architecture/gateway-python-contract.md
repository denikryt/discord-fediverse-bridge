# Gateway/Python contract

This document explains the internal API contract between the Fedify gateway and the Python bridge in both directions. It owns payload categories, authentication expectations, and route-level side effects; exact validation remains in source types.

## Gateway -> Python

The gateway uses one authenticated internal bridge API. `POST /internal/activitypub/events` delivers normalized activities; `/internal/fedify/*` supplies actor identities, signing keys, subscribers, published objects, mappings, and operator subscription rows. Authentication uses the `FEDIFY_SHARED_SECRET` bearer token. The Python bridge is the only database owner.

Common fields: `event_type`, `delivery_id`, `occurred_at`, `actor_id`, `community_actor_id`, and `object` or event-specific object payload.

Supported normalized event categories: `post.created`, `post.updated`, `post.deleted`, `comment.created`, `comment.updated`, `comment.deleted`, `follow.accepted`, `local.follow_requested`, `local.unfollow_requested`.

## Python -> Gateway

`src/fedify_gateway_client.py` calls authenticated internal gateway routes. Python decides bridge policy and persistence; the gateway owns actor documents, object URLs, HTTP signatures, and signed federation delivery.

| Route | Python caller method | Purpose | Important payload fields | Result or side effect |
| --- | --- | --- | --- | --- |
| `/prepare-community-follow` | `prepare_community_follow` | Verify canonical Group and allocate Follow identity without sending | `communityActorUrl` | Returns verified `communityActorUrl`, `communityInboxUrl`, and immutable `followActivityId` |
| `/send-prepared-community-follow` | `send_prepared_community_follow` | Submit only an already committed Follow | `communityActorUrl`, `communityInboxUrl`, `followActivityId` | Sends exactly the reserved Follow; timeout outcome remains unresolved in Python |
| `/follow-community` | `follow_community` | Legacy manual gateway send (not used by subscription operation) | `communityActorUrl` | Returns community actor URL, inbox URL, and Follow activity id |
| `/unfollow-community` | `unfollow_community` | Send Undo(Follow) to original saved Group inbox | `communityActorUrl`, `followActivityId`, optional `communityInboxUrl` from verified persisted Follow | Returns `ok` only after gateway send succeeds; Python claims `undo_sending` before calling and keeps ambiguous outcome unresolved |
| `/publish` | `publish_content` | Publish remote-community content | `actorUsername`, `communityActorUrl`, `kind`, `bodyMarkdown` | Returns canonical ids |
| `/publish-local-community` | `publish_local_community_content` | Publish local-community content | publish fields | Returns ids and delivery counts |
| `/send-local-community-relay` | `send_local_community_relay` | Deliver rendered relay activities | `signingActorUrl`, `deliveries[]` | Returns per-target outcomes |
| `/accept-local-community-follow` | `accept_local_community_follow` | Send Accept(Follow) | community, remote actor, inbox, Follow id | Sends signed Accept |
| `/update` | `update_content` | Send Update | actor, community, object, kind, body | Sends Update |
| `/delete` | `delete_content` | Send Delete | actor, community, object | Sends Delete |

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
