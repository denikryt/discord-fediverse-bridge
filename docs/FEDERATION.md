# Federation support

This document describes the federation profile implemented by this project. It explains which ActivityPub, ActivityStreams, WebFinger, Lemmy/threadiverse, and Mastodon-compatible behaviors are intentionally supported, and which parts of the broader fediverse protocol surface are outside the project's scope.

## Summary

This project is not a complete generic ActivityPub server. It implements a limited ActivityPub server-to-server bridge profile for Discord ↔ Lemmy/threadiverse federation, with selected Mastodon-compatible behavior for local-community replies.

The bridge should be described as:

```text
Limited ActivityPub S2S + ActivityStreams 2.0 profile for Lemmy/threadiverse interop,
with WebFinger discovery and partial Mastodon-compatible local-community relay.
```

It should not be described as:

```text
A full ActivityPub implementation.
A full Mastodon-compatible server.
A complete FEP-1b12 implementation.
An ActivityPub client-to-server implementation.
```

## Protocol layers used

The project uses these protocol layers and fediverse conventions:

- ActivityStreams 2.0 JSON-LD vocabulary for actors, objects, and activities.
- ActivityPub server-to-server delivery patterns for inbox delivery and actor/object dereferencing.
- WebFinger discovery for `acct:` handles that resolve to ActivityPub actor documents.
- HTTP Signatures and signed inbox delivery as practical fediverse interoperability requirements.
- Lemmy/threadiverse group-federation conventions for communities, posts, comments, and community-owned `Announce` fanout.
- A small Mastodon-compatible subset for handling `Note` replies into local communities.

The Python bridge owns orchestration, database state, Discord behavior, and routing policy. The Fedify gateway owns public ActivityPub/WebFinger routes, actor and object serving, inbound ActivityPub normalization, and signed outbound federation delivery.

## Supported actors

The gateway serves these ActivityPub actor categories:

| Actor category | ActivityStreams type | Example route | Purpose |
| --- | --- | --- | --- |
| Bridge actor | `Service` | `/actors/services/{identifier}` | Shared federation actor used for remote community follow/publish flows. |
| Registered Discord user | `Person` | `/actors/users/{username}` | Per-user ActivityPub identity for registered Discord users. |
| Local community | `Group` | `/actors/communities/{slug}` | Discord forum channel exposed as a local ActivityPub community. |

Actor documents provide stable identity and routing fields plus editable registered-user profile data. User `Person` actors expose immutable `preferredUsername`, editable `name` and escaped `summary`, optional `icon` and `image` objects backed by bridge-owned immutable media URLs, and up to four ordered `PropertyValue` attachments. Successful profile edits also create a durable complete `Update(Person)` and deliver it to the unique accepted follower endpoints.

Registered User Actor followers are auto-accepted and stored separately from local-community subscribers. Their canonical `/actors/users/{username}/followers` collection publicly exposes accepted remote actor IDs through bounded pages. User-authored public content is delivered to the accepted follower endpoint snapshot through durable per-target retry. User outboxes remain empty placeholders until the dedicated outbox work.

A bound personal-feed guild text channel publishes only owner-authored root messages. Each accepted root becomes one bridge-owned public `Create(Note)` with deterministic object/activity IDs, a canonical `published` value derived from the Discord message creation time, Public addressing, and the User Actor followers collection in `cc`. Replies use a lazily adopted or created public Discord thread whose snowflake equals the root message ID. Registered Discord users publish replies as their own `Person` actors; unregistered users are rejected without federation. Inbound remote comments are stored canonically before Discord delivery, and remote Update/Delete activities must match the stored exact actor owner before they may edit or remove the mapped Discord message.

Discord synchronization is active only while the root mapping's channel remains the User Actor's current personal-feed binding. After unbind or move, inbound comments remain stored but Discord rendering and outbound thread publication are suppressed. Discord rendering uses at most three actual API attempts with retry intervals no longer than 30 seconds; waiting for an out-of-order parent does not consume that budget. Attachments, private visibility, complete remote conversation reconstruction, and public outbox pagination remain outside personal feed conversations.

## Supported discovery

The gateway supports WebFinger actor discovery for bridge, user, and community handles.

Supported examples include:

```text
acct:bridge@example.com
acct:alice@example.com
acct:!community@example.com
acct:community@example.com
```

The WebFinger response points to the canonical ActivityPub actor URL through a `self` link with ActivityPub JSON media type.

WebFinger support is limited to actor discovery needed by this bridge. It is not a general-purpose WebFinger identity service.

The gateway also exposes one bridge-specific discovery endpoint:

```text
GET /.well-known/discord-fediverse-bridge/communities
```

This endpoint returns public local-community identity fields for `/subscribe-channel`
discovery. It is not ActivityPub, not WebFinger, and not a claim of Lemmy API
compatibility.

## Supported inbound ActivityPub

The inbound federation profile is intentionally narrow. The gateway accepts selected ActivityPub activities, normalizes them into the Python bridge's internal event model, and forwards them to Python through the private `/internal/activitypub/events` route.

Supported inbound activity patterns include:

| Inbound pattern | Bridge meaning |
| --- | --- |
| `Create(Page)` | Remote post-compatible object. |
| `Create(Article)` | Remote post-compatible object when a peer uses `Article`. |
| `Create(Note)` | Remote comment-compatible object. |
| `Update(Page|Article|Note)` | Direct inbound edit for supported post or comment objects. |
| `Delete` | Direct inbound delete for supported post or comment objects. |
| `Announce(Create(Page|Article|Note))` | Lemmy/threadiverse community fanout for post or comment creation. |
| `Announce(Update(Page|Article|Note))` | Lemmy/threadiverse edit fanout. |
| `Announce(Delete(...))` | Lemmy/threadiverse delete fanout. |
| `Follow` targeting a local community `Group` | Remote actor requests to follow a Discord-backed local community. |
| `Undo(Follow)` targeting a local community `Group` | Remote actor unfollows a Discord-backed local community. |
| `Accept(Follow)` | Remote instance accepted a bridge actor follow request. |

For wrapped `Announce(Create/Update/Delete)`, the gateway processes the payload
attached to the Fedify-accepted delivery, rather than a shared raw cache entry
indexed by the outer ID. It checks the complete nested content against the
typed accepted `Announce` and its object ID before dereferencing the author's
origin. A later inbox request with the same outer and nested IDs cannot replace
the signed content. The fetched original must still match the claimed author,
object identity, recipients, and supported content fields.

Inbound `Undo(Follow)` also uses the payload attached to its authenticated
Fedify delivery. Before producing a local unfollow event, the gateway checks
that the accepted outer Undo and embedded Follow IDs, actor, and target match
Fedify's typed activity. A later request reusing the Undo ID cannot change
the local actor whose subscription is removed.

Inbound `Accept(Follow)` is accepted for bridge subscription state only when
its authenticated object identifies an outbound Follow. A URL reference is
matched to the stored Follow ID by Python. An embedded activity must be a
Follow by the bridge service actor targeting the Accept actor; an embedded
Follow without an ID is ignored. The gateway does not fill a missing ID from
the shared raw request cache. Python also requires the Accept actor to own
the stored pending Follow before activating subscriptions.

The project does not provide generic inbox processing for every ActivityStreams activity type.

Unsupported or out-of-scope inbound behavior includes, unless explicitly added by a future plan:

```text
Like
Undo(Like)
Reject
Add
Remove
Block
Flag
Move
Question / polls
EmojiReact
full generic Announce/boost semantics
full Tombstone dereference handling
arbitrary ActivityStreams object routing
```

## Supported outbound ActivityPub

The bridge sends selected activities required by the supported Discord ↔ fediverse workflows.

Supported outbound activity patterns include:

| Outbound pattern | Bridge meaning |
| --- | --- |
| `Follow` | Bridge actor follows a remote community. |
| `Undo(Follow)` | Bridge actor unfollows a remote community. |
| `Accept(Follow)` | Local community accepts a remote subscriber. |
| `Create(Page)` | Discord forum thread published as a post-compatible object. |
| `Create(Note)` | Discord message/reply published as a comment-compatible object. |
| `Update(Page|Note)` | Edit propagation for supported post/comment objects. |
| `Delete` | Delete propagation for supported post/comment objects. |
| `Announce(Create(...))` | Local community relays supported content to remote subscribers. |
| `Announce(Update(...))` | Local community relays supported edits to remote subscribers. |
| `Announce(Delete(...))` | Local community relays supported deletes to remote subscribers. |

Some outbound delivery uses Fedify's federation APIs. Some compatibility paths use manually rendered ActivityStreams JSON and signed HTTP delivery where the bridge needs precise Lemmy/threadiverse-compatible payload shapes.

## Lemmy and threadiverse assumptions

The bridge is primarily designed for Lemmy/threadiverse interoperability. Several behaviors are intentionally vendor- or ecosystem-specific rather than generic ActivityPub behavior.

Important assumptions include:

- A community is represented as an ActivityPub `Group` actor.
- Posts are represented as `Page` or `Article`-compatible objects.
- Comments are represented as `Note` objects.
- Community fanout is represented through community-owned `Announce` activities.
- Remote Lemmy object URLs may contain `/post/{id}` or `/comment/{id}` and may be parsed for compatibility metadata.
- Local communities expose ActivityPub Group actors under `/actors/communities/{slug}`; retired `/communities/{slug}` and `/c/{slug}` local actor aliases are not compatibility routes.
- Compatibility rendering avoids JSON-LD compaction forms that known Lemmy receivers reject.

These assumptions are not required by the base ActivityPub specification. They are bridge-specific interoperability choices.

## Mastodon compatibility

Mastodon compatibility is partial. Registered user actors expose Mastodon-compatible display name, bio, avatar, header, and up to four profile metadata fields; local-community reply interoperability remains supported.

The bridge can handle selected Mastodon-style `Create(Note)` replies that target local communities, and local community relay code can render compatibility payloads for selected reply flows.

The project does not implement a full Mastodon-compatible server profile. Notable unsupported Mastodon-oriented features include:

```text
NodeInfo
featured / featuredTags collections
full Like / boost / notification semantics
polls
custom emoji handling
full Mention and Tag behavior
broad HTML/content negotiation behavior
complete public timeline or profile collection behavior
```

## Not supported

The project does not support the ActivityPub Client-to-Server API. External clients cannot use the bridge as a generic ActivityPub server by posting arbitrary activities to an actor outbox.

The project also does not claim full conformance to any broad fediverse software profile. It does not implement every ActivityPub activity type, every ActivityStreams object type, every Mastodon extension, or a complete FEP-1b12 group-federation profile.

Unsupported by design:

```text
ActivityPub Client-to-Server API
Generic ActivityPub server behavior
Generic inbox side effects for all ActivityStreams activity types
Full Mastodon-compatible server behavior
Full FEP-1b12 conformance claim
Complete historical outbox/followers collections
NodeInfo or broad fediverse software metadata
```

## Internal bridge model is not ActivityPub

The gateway translates inbound ActivityPub into normalized Python events. Those events are an internal bridge contract, not an ActivityPub representation.

The internal event model exists so Python runtime code can make bridge decisions using stable fields such as event type, actor ID, community actor ID, object URL, content, parent information, and delivery metadata. It should not be treated as a public protocol.

Likewise, Python-to-gateway HTTP actions are internal commands. They are not ActivityPub Client-to-Server endpoints.

## Accurate wording for documentation and releases

Use wording like this when describing federation support:

```text
This project implements a limited ActivityPub server-to-server bridge profile.
It uses ActivityStreams 2.0 JSON-LD objects and WebFinger discovery, with a
Fedify-based gateway for actor serving, inbox handling, HTTP-signature delivery,
and selected ActivityPub activities.

The supported profile is focused on Lemmy/threadiverse community federation,
with partial Mastodon-compatible handling for local-community Note replies.
```

Avoid wording like this unless the project grows a broader protocol surface:

```text
This project fully supports ActivityPub.
This project is a Mastodon-compatible server.
This project implements all of FEP-1b12.
This project is a generic fediverse server.
```

## Implementation reference points

Relevant implementation areas:

- `fedify-gateway/src/server.ts` — public gateway routes and internal gateway action routes.
- `fedify-gateway/src/federation.ts` — Fedify integration and inbound activity handling.
- `fedify-gateway/src/outbound/*` — outbound federation use-case modules for signed JSON transport, follow publication, content publication, local-community fanout, personal-feed rendering, content mutations, relay delivery, and User Actor delivery batches.
- `fedify-gateway/src/federation-outbound.ts` — compatibility export layer for existing gateway imports.
- `fedify-gateway/src/normalize.ts` — ActivityPub-to-Python normalized event conversion.
- `fedify-gateway/src/actors.ts` and `fedify-gateway/src/webfinger.ts` — actor rendering and discovery.
- `src/activitypub_models.py` — Python internal event models.
- `src/activitypub_handlers.py` — Python dispatch from normalized ActivityPub events to bridge runtimes.
- `src/local_communities/activitypub_renderers.py` — local-community compatibility object rendering.
- `src/fedify_gateway_client.py` — Python-to-gateway internal command client.

## Registered-user profile update delivery

Successful registered-user profile mutations create one immutable logical profile version and one durable `Update(Person)` source in the same SQLite transaction as the local profile change. The transaction also fixes the accepted follower audience, deduplicates exact shared or individual inbox endpoints, snapshots follower relationship incarnation IDs, and retains referenced profile media until the source is no longer transportable.

The Python runtime owns source state, retry timing, endpoint claims, ordering, and per-target outcomes. The Fedify gateway renders the complete `Person` through the same actor builder used by the public actor route and performs signed endpoint delivery as the owning User Actor. Retry uses the persisted activity JSON and stable activity ID; it does not re-read a newer profile into an older source.

Profile delivery is partial-failure tolerant. A failed endpoint does not roll back the local profile or block healthy endpoints. Account settings report local success separately from pending remote refresh. Existing user-authored content recipients are unchanged in local subscriber participation.

## User Actor content follower delivery

Public user-authored `Create`, `Update`, and `Delete` activities keep their existing canonical object and activity identities and existing community/conversation delivery. The same canonical activity JSON is also stored by Python and delivered asynchronously to the accepted followers of the author, grouped by exact inbox or shared-inbox endpoint. Local-community `Announce` remains a distinct community-owned envelope; it is not replaced by direct User Actor delivery.

Create persistence snapshots the accepted follower relationships in the same database transaction as the published object and Discord mapping. Content mutations use durable desired state and a stable pre-gateway reservation so duplicate or concurrent Discord events cannot allocate a second logical mutation identity. The gateway remains the signing and canonical serialization boundary, while Python owns restart recovery, endpoint eligibility, policy checks, retry state, and partial-failure isolation. A delivered terminal `Delete` prevents later stale `Create` or `Update` delivery for the same object and endpoint.

## User Actor outbox

`/actors/users/{username}/outbox` is the canonical `OrderedCollection`. Fixed pages contain current `Create` activities for durable undeleted public posts and comments, ordered by publication time and durable row id. Separate page requests are not an immutable snapshot.
