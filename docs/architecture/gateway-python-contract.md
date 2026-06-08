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
| `/follow-community` | `follow_community` | Send shared bridge actor Follow | `communityActorUrl` | Returns community actor URL, inbox URL, and Follow activity id |
| `/unfollow-community` | `unfollow_community` | Send Undo(Follow) | `communityActorUrl`, `followActivityId` | Returns `ok` or error |
| `/publish` | `publish_content` | Publish remote-community content | `actorUsername`, `communityActorUrl`, `kind`, `bodyMarkdown` | Returns canonical ids |
| `/publish-local-community` | `publish_local_community_content` | Publish local-community content | publish fields | Returns ids and delivery counts |
| `/send-local-community-relay` | `send_local_community_relay` | Deliver rendered relay activities | `signingActorUrl`, `deliveries[]` | Returns per-target outcomes |
| `/accept-local-community-follow` | `accept_local_community_follow` | Send Accept(Follow) | community, remote actor, inbox, Follow id | Sends signed Accept |
| `/update` | `update_content` | Send Update | actor, community, object, kind, body | Sends Update |
| `/delete` | `delete_content` | Send Delete | actor, community, object | Sends Delete |

## Bridge signing key ownership

Python initializes the bridge actor key in `bridge_actor_keys` before readiness. The gateway reads that row from the shared SQLite database; it does not generate an in-memory bridge identity or require bridge JWK environment variables during normal runtime.
