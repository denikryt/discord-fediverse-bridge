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
| Bridge actor | `Service` | `/actors/bridge` | Shared federation actor used for remote community follow/publish flows. |
| Registered Discord user | `Person` | `/users/{username}` | Per-user ActivityPub identity for registered Discord users. |
| Local community | `Group` | `/communities/{slug}` and `/c/{slug}` | Discord forum channel exposed as a local ActivityPub community. |

Actor documents are intended to provide the fields needed by common fediverse peers: stable `id`, `preferredUsername`, `name`, `summary`, `inbox`, `outbox`, `followers`, shared inbox metadata, and public-key material.

The project does not implement full actor collection behavior. Some collection routes exist so peers have stable URLs, but outbox and followers collections are minimal and are not a complete historical activity archive.

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
| `Announce(Create(Page|Article|Note))` | Lemmy/threadiverse community fanout for post or comment creation. |
| `Announce(Update(Page|Article|Note))` | Lemmy/threadiverse edit fanout. |
| `Announce(Delete(...))` | Lemmy/threadiverse delete fanout. |
| `Follow` targeting a local community `Group` | Remote actor requests to follow a Discord-backed local community. |
| `Undo(Follow)` targeting a local community `Group` | Remote actor unfollows a Discord-backed local community. |
| `Accept(Follow)` | Remote instance accepted a bridge actor follow request. |

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
| `Accept(Follow)` | Local community accepts a remote follower. |
| `Create(Page)` | Discord forum thread published as a post-compatible object. |
| `Create(Note)` | Discord message/reply published as a comment-compatible object. |
| `Update(Page|Note)` | Edit propagation for supported post/comment objects. |
| `Delete` | Delete propagation for supported post/comment objects. |
| `Announce(Create(...))` | Local community relays supported content to followers. |
| `Announce(Update(...))` | Local community relays supported edits to followers. |
| `Announce(Delete(...))` | Local community relays supported deletes to followers. |

Some outbound delivery uses Fedify's federation APIs. Some compatibility paths use manually rendered ActivityStreams JSON and signed HTTP delivery where the bridge needs precise Lemmy/threadiverse-compatible payload shapes.

## Lemmy and threadiverse assumptions

The bridge is primarily designed for Lemmy/threadiverse interoperability. Several behaviors are intentionally vendor- or ecosystem-specific rather than generic ActivityPub behavior.

Important assumptions include:

- A community is represented as an ActivityPub `Group` actor.
- Posts are represented as `Page` or `Article`-compatible objects.
- Comments are represented as `Note` objects.
- Community fanout is represented through community-owned `Announce` activities.
- Remote Lemmy object URLs may contain `/post/{id}` or `/comment/{id}` and may be parsed for compatibility metadata.
- Local communities expose Lemmy-style community routes such as `/communities/{slug}` and `/c/{slug}`.
- Compatibility rendering avoids JSON-LD compaction forms that known Lemmy receivers reject.

These assumptions are not required by the base ActivityPub specification. They are bridge-specific interoperability choices.

## Mastodon compatibility

Mastodon compatibility is partial and local-community-focused.

The bridge can handle selected Mastodon-style `Create(Note)` replies that target local communities, and local community relay code can render compatibility payloads for selected reply flows.

The project does not implement a full Mastodon-compatible server profile. Notable unsupported Mastodon-oriented features include:

```text
NodeInfo
featured / featuredTags collections
Mastodon profile extension fields as a complete set
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
- `fedify-gateway/src/federation-outbound.ts` — outbound federation delivery and compatibility rendering.
- `fedify-gateway/src/normalize.ts` — ActivityPub-to-Python normalized event conversion.
- `fedify-gateway/src/actors.ts` and `fedify-gateway/src/webfinger.ts` — actor rendering and discovery.
- `src/activitypub_models.py` — Python internal event models.
- `src/activitypub_handlers.py` — Python dispatch from normalized ActivityPub events to bridge runtimes.
- `src/local_communities/activitypub_renderers.py` — local-community compatibility object rendering.
- `src/fedify_gateway_client.py` — Python-to-gateway internal command client.
