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

Registered User Actor followers are auto-accepted and stored separately from local-community subscribers. Their canonical `/actors/users/{username}/followers` collection publicly exposes accepted remote actor IDs through bounded pages. User-authored public content is delivered to the accepted follower endpoint snapshot through durable per-target retry. User outboxes remain empty placeholders until the dedicated outbox stage.

A bound personal-feed guild text channel publishes only owner-authored root messages. Each accepted root becomes one bridge-owned public `Create(Note)` with deterministic object/activity IDs, a canonical `published` value derived from the Discord message creation time, Public addressing, and the User Actor followers collection in `cc`. Replies use a lazily adopted or created public Discord thread whose snowflake equals the root message ID. Registered Discord users publish replies as their own `Person` actors; unregistered users are rejected without federation. Inbound remote comments are stored canonically before Discord delivery, and remote Update/Delete activities must match the stored exact actor owner before they may edit or remove the mapped Discord message.

Discord synchronization is active only while the root mapping's channel remains the User Actor's current personal-feed binding. After unbind or move, inbound comments remain stored but Discord rendering and outbound thread publication are suppressed. Discord rendering uses at most three actual API attempts with retry intervals no longer than 30 seconds; waiting for an out-of-order parent does not consume that budget. Attachments, private visibility, complete remote conversation reconstruction, and public outbox pagination remain outside Stage 8.

### Local community inbound Follow generation safety

For local Bridge-hosted `Group` subscriptions, a verified incoming
`Follow(new)` establishes one accepted remote subscriber generation. A repeated
`Follow(current)` can repeat its signed Accept using the persisted target inbox.
After a persisted failed Accept, a retry of an older Follow cannot overwrite a
newer accepted generation, and a retry after `Undo` cannot recreate a deleted
subscriber. These are terminal `ignored_already_applied` decisions; the remote
actor must send a genuinely new verified Follow ID to subscribe again.

`Undo(Follow)` must reference the exact non-empty currently accepted Follow ID;
a late `Undo(old)` or missing ID is ignored without affecting current access.
This is the *incoming* local-community subscriber lifecycle, not the outbound
NodeBB Follow/Undo state machine.

### Bridge Service and registered Person authorization

When a remote Bridge `Service` follows a locally hosted Group, a registered
`Person` from that Bridge can author a direct signed Create without obtaining
a separate accepted Follow. The gateway records the authenticated **outer**
activity sender; the local Group checks the exact accepted Service relationship,
public `Service`/`Person` actor documents and their shared Bridge namespace.
Service actor documents advertise `Bridge software: discord-fediverse-bridge`
as public ActivityStreams `PropertyValue` metadata. A mere matching hostname
or an unsigned inner Create is not permission. Existing same-actor subscribers
are unchanged. Until both Bridges expose the declaration, the new
Service→Person path refuses access rather than guessing software provenance.

For locally hosted Group **Update/Delete**, the verified direct action must be
signed by the original Person even when the current accepted subscription
belongs to its Bridge Service. The first Create fixes the local Group, AP
object, owner, origin subscriber and (for replies) root/parent. These fields
are checked again before any Discord edit/delete or remote relay. A Group-only
`Announce(Update|Delete)`, later conflicting `attributedTo`, another Group's
placement or a historical missing owner cannot grant modification rights.
An ID-only signed `Delete(Tombstone)` may omit its ancestry, but it cannot
change the original Group/parent association. Moderator delegation without a
separately verified authority proof remains unsupported.

The first accepted inbound Create also fixes the rendered Discord title/body
in the canonical local host row before the Discord REST call. A later signed
Create or `Announce` with the same object ID may complete an unfinished sibling
delivery, but uses that saved rendering rather than a changed replay body.
Historical host rows without first-Create content evidence cannot reconstruct
new sibling content from the latest envelope and fail closed for that retry.

### Canonical relay of locally hosted Group content

The first authorized inbound Create fixes a relay source for
`(local_community_id, source_object_ap_id)` regardless of how many distinct
`Announce.id` wrappers later deliver the same object. Its author, original
Service subscriber and first verified payload remain immutable. Relay excludes
that **Service**, not the author Person, from accepted targets. The initial
normalized Note projection is stored with the verified source when required for
Mastodon→threadiverse rendering; a later envelope cannot replace its body or
parent. Update/Delete remain separate signed revision identities, authorized
against the same original local Group and immutable Person owner.

Each relay target has one persistent Announce ID and a SQLite CAS-claimed send.
The gateway signs and sends that prepared activity without regenerating its ID.
A confirmed delivery is never sent again; a confirmed **pre-network** rejection
may retry the same ID and payload. HTTP timeout, lost response, a partial batch
without an outcome, or a gateway send exception is `unknown` and is **not**
retried automatically. A crash after `sending` can also mean the destination
already accepted the activity; this release does not supply a general relay
reconciliation worker or exactly-once delivery guarantee.

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

The `/subscribe-community` direct-instance autocomplete checks public Bridge
community summaries by slug, name, title, `!handle@host` (or `handle@host`),
canonical `/communities/{slug}` and alternate `/c/{slug}` actor URLs. Results
from this endpoint use `bridge-local:` or `bridge-remote:` selection values;
remote database numeric IDs are not used as local IDs. The 25-result Discord
list has a 100-character limit on names and values. A long actor URL that
cannot be encoded losslessly is omitted from autocomplete; moderators may
enter its full URL manually. An unavailable Bridge endpoint falls back to the
existing Lemmy discovery path; a confirmed Bridge response does not.
Autocomplete errors and empty results are logged with normalized origin and
one bounded reason code only; blocked remote origins are rejected before the
network request. When the Bridge endpoint is unavailable, NodeBB autocomplete
reads anonymous `GET /api/categories` once, traverses its nested category tree,
and filters the listing locally. It does **not** fetch each category actor or
claim the suggestions are verified federation targets. NodeBB's `isSection` is
a display-only flag, not a visibility or federation denial: public section
parents and their children appear as candidates. Explicitly private, disabled,
hidden or nonfederated parents and their descendants are excluded. A displayed
candidate may still fail guest `read` privilege or Group validation.

On **command submission**, `nodebb:` selections and manually entered category
links are revalidated against current policy, `/api/category/{cid}`, and the
canonical ActivityPub `Group` actor/inbox at `/category/{cid}` **before** any
Follow or Discord placement. NodeBB handles resolve through WebFinger. Choice
values are only untrusted category locators, never actor identity proof. NodeBB
selection uses `remote_nodebb` with no Lemmy numeric ID. Discord returns at most
25 suggestions per response, but text filtering searches the full received
listing. The tree traversal supports up to 4096 nodes and 24 nested levels;
exceeding either limit returns an explicit discovery error, never a silently
truncated list. Long candidate URLs receive short-lived autocomplete lookup
keys; an expired key is rejected and the user must search again or enter the
category URL. NodeBB HTTP reads enforce HTTPS,
current federation policy, public-IP DNS, same-origin redirects, and bounded
response size. The NodeBB Follow/Undo, signed Group announcement ingestion, bounded historical
root membership, durable Create/direct delivery and owner-checked mutations are
implemented on this branch. These paths have focused fixture/SQLite tests, but
**live NodeBB–Fedify–Discord end-to-end interoperability is not verified**
without the actual gateway and Discord dependencies. Discovery alone does not
prove remote acceptance of a Follow or publication.

Remote subscriptions now reserve the canonical Group actor, verified inbox,
immutable outbound Follow activity ID, software profile, and the first Discord
channel binding in one SQLite transaction **before** invoking the Fedify
sender. A second channel reuses that generation; the sender uses a durable
`prepared → sending → sent` claim so only one command sends the reserved ID.
For **Follow only**, the gateway uses Fedify `sendActivity(..., { immediate: true })`
and bypasses its in-process outbox queue: `sent` now means the remote inbox
returned an HTTP success for that Follow, **not** that the remote Group
accepted the subscription. An explicit signed `Accept` remains required for
NodeBB; other outbound federation messages may still use the normal Fedify
queue. The Follow identity and target are durable in SQLite before this
synchronous send, so no queue task can vanish on gateway restart after Python
mistakenly marks it `sent`.
A signed `Accept(Follow)` must identify that exact activity and originate from
its original Group; an early Accept is not reverted by a later HTTP result.
Only a confirmed `threadiverse` profile may be implicitly accepted on inbound
content. NodeBB and `unknown` require explicit signed Accept. Both the
prepared and bootstrap Follow sender pass Fedify the service **identifier**
(`services/bridge`, not the public WebFinger username `bridge`) so Fedify uses
the persisted service key without running the handle mapper. Sender-lookup
failures are logged by the gateway and Python with the original Follow ID.
The gateway performs an explicit Service-key check **before remote HTTP** and
returns HTTP 422 with `code=follow_sender_unavailable` and `dispatched=false`
if that check fails. Only this authenticated structured result is proof of a
non-dispatched Follow: Python atomically releases `sending → prepared`, retains
the original Follow ID and channel binding, and reports
`follow_sender_resolution_failed`. After correcting the key/configuration, a
moderator can invoke `/subscribe-community` again for the **same channel and
Group**. The command CAS-claims and sends the original Follow instead of
allocating a new ID. If the last channel was cancelled in the meantime, the
pre-HTTP failure removes the never-sent generation without an Undo. A generic
HTTP 500, even one containing `No actor found`, is **not** accepted as proof
by text matching. A possible network send with no response remains
`sending`/pending and must not be retried blindly. `prepared` is durable across
restart; stale `sending` from previous versions is **not** reclassified just
because the gateway was upgraded. This release does not add a background
Follow retry scheduler: retries of proved local failures are on demand.
 The last Discord channel unsubscribes
under the same SQLite writer boundary: an unclaimed `prepared` Follow is
cancelled without HTTP; a `sending` Follow retains `cancel_requested` until
the original sender has a confirmed result; a proven `sent` Follow can claim
`undo_sending` **before** signed `Undo(Follow)` goes to the original saved
inbox. The Undo ID is derived deterministically from the original Follow ID.
A resubscribe before Undo begins atomically reuses the original generation;
`undo_sending` is not reactivated on restart. Neither a pending Follow nor an
unresolved Undo proves the remote Group accepted or removed the subscription.

### Recovery of a pre-upgrade Follow stuck in `sending`

A `sending` Follow recorded by an older gateway can represent a local
pre-network failure **or** an HTTP request with unknown outcome. Installing a
new binary does not distinguish these cases. If the original gateway log and
corresponding Follow ID prove the failure was strictly before any remote HTTP
(e.g. the old `No actor found for the given username "services/bridge"` from
Fedify sender resolution), an operator may restore **that exact generation** to
`prepared`. Otherwise leave it unresolved and verify state with the remote
administrator; lack of an `Accept` alone is not proof of non-delivery.

Stop all bridge processes and restart supervisors, create and restore-validate
an offline SQLite+media backup, and verify the exact stored Group URL, Follow
ID, pending channel binding and original gateway evidence. With the old
process stopped, substitute the **verified** actor and Follow ID below and run
in SQLite:

```sql
BEGIN IMMEDIATE;
UPDATE bridge_actor_follows
SET follow_dispatch_state = 'prepared'
WHERE community_actor_id = 'https://nodebb.example/category/42'
  AND follow_activity_id = 'https://bridge.example/actors/services/bridge/activities/follow/EXACT'
  AND follow_dispatch_state = 'sending'
  AND status = 'pending'
  AND cancel_requested = 0
  AND EXISTS (
    SELECT 1 FROM channel_community_subscriptions AS c
    WHERE c.lemmy_community_actor_id = bridge_actor_follows.community_actor_id
      AND c.follow_activity_id = bridge_actor_follows.follow_activity_id
      AND c.status = 'pending'
  );
SELECT changes();
-- COMMIT only if changes() = 1 and all preconditions/evidence were verified;
-- otherwise ROLLBACK. Do not run if remote HTTP may have been attempted.
```

After committing and restarting, repeat `/subscribe-community` in the
existing pending Discord channel. It will reuse the original ID and the
saved target; a fresh remote `Accept` still governs activation. This is an
exceptional offline repair of a **proven** local failure, not a general retry
procedure for timeouts or an unresolved `Undo`.

### Exceptional offline recovery for an unresolved remote Undo

This procedure applies **only** when `/subscribe-community` reports
`remote_follow_cleanup_unresolved` for an exact Group and Follow. Stop **all**
bridge runtimes and their restart supervisors. Take and restore-validate a
protected SQLite + media backup. Obtain independent confirmation from the
remote Group administrator that the original bridge Service is **absent**
from that Group's subscribers. Without that proof, leave the row unchanged.

With all runtimes still stopped, inspect the saved Group and Follow IDs,
check that no channel bindings remain, and run this guarded transaction with
the **literal exact ActivityPub URLs** substituted:

```sql
BEGIN IMMEDIATE;
SELECT community_actor_id, follow_activity_id, cancel_requested, follow_dispatch_state
FROM bridge_actor_follows
WHERE community_actor_id = 'https://nodebb.example/category/42'
  AND follow_activity_id = 'https://bridge.example/actors/services/bridge/activities/follow/EXACT';
SELECT COUNT(*) FROM channel_community_subscriptions
WHERE lemmy_community_actor_id = 'https://nodebb.example/category/42';
DELETE FROM bridge_actor_follows
WHERE community_actor_id = 'https://nodebb.example/category/42'
  AND follow_activity_id = 'https://bridge.example/actors/services/bridge/activities/follow/EXACT'
  AND cancel_requested = 1 AND follow_dispatch_state = 'undo_sending'
  AND NOT EXISTS (
    SELECT 1 FROM channel_community_subscriptions
    WHERE lemmy_community_actor_id = 'https://nodebb.example/category/42'
  );
SELECT changes();
-- COMMIT only when changes() = 1 and the earlier COUNT(*) = 0.
-- Otherwise ROLLBACK and investigate; do not retry a possibly delivered Undo.
COMMIT;
```

After a successful guarded deletion, admit v1 read-only under the normal
startup lock and restart. A fresh `/subscribe-community` then reserves a new
Follow generation. Never delete an uncertain Follow or bypass these predicates
without evidence of remote unsubscribe.

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

The project does not provide generic inbox processing for every ActivityStreams activity type.

**Inbox processing on pinned Fedify 2.2.0.** Incoming activities are handled by
Fedify's authenticated listener on the request path, rather than its built-in
inbox worker. This is necessary because the 2.2.0 queue worker deduplicates
again by `(origin, activity.id)` and ignores the custom per-inbox idempotency
strategy: a signed NodeBB Group `Announce` replay would otherwise be lost before
Python could finish an eligible failed Discord target. The verified callback
still controls which activity reaches Python; Python's receipt/canonical CAS
preserves completed/unknown sends without blind resend. Other inbound events
retain their per-inbox idempotency keys. HTTP POST still returns 202 for
accepted activity, but now waits for the verified inbox listener to finish.
Outgoing activity and fanout remain queued using the existing in-process queue;
no new transport or background recovery worker is introduced.

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

Profile delivery is partial-failure tolerant. A failed endpoint does not roll back the local profile or block healthy endpoints. Account settings report local success separately from pending remote refresh. Existing user-authored content recipients are unchanged in Stage 4.

## User Actor content follower delivery

Public user-authored `Create`, `Update`, and `Delete` activities keep their existing canonical object and activity identities and existing community/conversation delivery. The same canonical activity JSON is also stored by Python and delivered asynchronously to the accepted followers of the author, grouped by exact inbox or shared-inbox endpoint. Local-community `Announce` remains a distinct community-owned envelope; it is not replaced by direct User Actor delivery.

Create persistence snapshots the accepted follower relationships in the same database transaction as the published object and Discord mapping. Content mutations use durable desired state and a stable pre-gateway reservation so duplicate or concurrent Discord events cannot allocate a second logical mutation identity. The gateway remains the signing and canonical serialization boundary, while Python owns restart recovery, endpoint eligibility, policy checks, retry state, and partial-failure isolation. A delivered terminal `Delete` prevents later stale `Create` or `Update` delivery for the same object and endpoint.

## User Actor outbox

`/actors/users/{username}/outbox` is the canonical `OrderedCollection`. Fixed pages contain current `Create` activities for durable undeleted public posts and comments, ordered by publication time and durable row id. Separate page requests are not an immutable snapshot.

## Community Create durable admission and direct Group delivery (CP15–CP16)

Remote Group publication renders immutable typed User Actor Create IDs before
network delivery. The renderer selects NodeBB Note roots/replies and retains
Lemmy/Bridge Page roots; unverified software profiles fail closed. The original
accepted Group/Follow inbox is snapshotted with the first Create in SQLite.
`queued` means the source was saved locally, **not** that the Group accepted it.
The Discord runtime records canonical thread/reply mappings and mirrors sibling
forums on this locally committed result, without waiting for network transport.
The User Actor delivery worker validates the original accepted Follow generation,
channel binding and Group/inbox policy for direct transport, including authors
with no User Actor followers. A follower at the same endpoint does not override
Group revocation; shared endpoints are sent at most once. A timeout after a
possible send persists `unknown` without automatic retry or a new Create ID.
The older follower-only delivery retry contract remains unchanged. Public
object GET and outbox reconstruct the original Create's `Note`/`Page` type and
NodeBB conversation `context` from the durable outbox source, not from mutable
Follow discovery. Direct NodeBB Update carries this same verified `context` in
its persisted signed JSON.
The persistent Group delivery and local fanout implementation is present;
release acceptance still requires the full dependency-backed integration tests.

## Direct Group mutations after queued Create (CP17)

Discord-origin edits/deletes of a remote Group publication preserve the Group
actor, inbox, Follow generation and content profile of its initial durable
Create. Mutation rendering does not send ActivityPub before SQLite commit;
after a confirmed original Create delivery, the existing User Actor worker
submits Update/Delete in revision order with that original target. If Create
is still queued or its network result is unknown, mutation delivery remains
pending rather than being reported as accepted by NodeBB. A cancelled Follow,
changed generation or missing historical direct recipient evidence does not
permit a mutation to rediscover a new inbox or use an unrelated follower.
The older local-community and personal-feed mutation paths are independent.
