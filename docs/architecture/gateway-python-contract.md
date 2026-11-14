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

A repeat of a signature-verified NodeBB `Announce` with the **same outer wire
ID** must reach this Python path again: Fedify's inbox idempotency skips its
usual `per-inbox` cache only for the NodeBB category-actor `Announce` shape.
The actor path is a routing hint, **not** authorization; the accepted Group,
policy, canonical owner and immutable first-Create snapshot are revalidated
before any Discord effect. A previously terminal global wire receipt remains
the historical outcome, while canonical per-forum CAS may complete only
missing or definitely failed-before-REST targets. Fully completed repeats
continue to report `duplicate`; ambiguous Discord sends are never blindly
retried. Other inbox activities retain the default per-inbox key strategy,
and local Group receipts remain tenant-scoped.

### Verified local Group delivery scope (CP19)

For **locally hosted Bridge Groups**, the Fedify-verified direct `Create`,
`Update`, `Delete` or verified `Announce(Create|Update|Delete)` is routed using
server-owned inbox context and the signed activity's addressing. An
actor-specific `/actors/communities/{slug}/inbox` admits only its verified
route owner, if that Group is explicitly addressed; `/inbox` resolves all
bounded, unambiguous local Group recipients independently. Missing route
provenance, nonexistent/contradictory Group identity, or an excessive recipient
set fails closed before any internal content HTTP delivery. Signed remote
NodeBB/Lemmy Group announcements still go through the remote distributor
subscription path; they do not need a local Group address.

Every admitted local Group content event carries the **server-derived**
`verified_destination_group_actor_id`, equal to `community_actor_id`. The
original outer wire `delivery_id` and `X-Bridge-Delivery-Id` are unchanged,
including `Announce(Create(...))` where the inner Create ID is merely
provenance. Python checks that the scope names an existing local Group before
receipt admission and indexes receipt lifecycle transitions by
`(delivery_id,event_type,verified_destination_group_actor_id)`. Old receipts
retain NULL scope, and Follow/personal/remote events retain the global
`delivery_id` receipt; a historical global receipt is never evidence for
skipping or admitting a different Group's Create. After admission, each Group still requires its independent subscriber ACL,
owner and canonical host reservation; receipt scope is not content authorization.

### Bridge Service→Person author admission (CP20)

For local Group content, `verified_sender_actor_id` is derived only from the
**outer Fedify-authenticated** ActivityPub actor. It must not be copied from an
inner `Announce(Create)` actor or from a raw ID-indexed request cache. For a
direct Create signed by a registered remote Bridge `Person`, Python checks the
outer sender against the `Create.actor`, original object ID and `attributedTo`
before any duplicate/parent/Discord/relay operation. The signed Person retains
its own author identity even when the authorization relationship belongs to
a different accepted `Service` subscriber.

A Service subscriber authorizes one such Person only when both are canonical
`https://origin/actors/{services,users}/{identifier}` actors, public documents
confirm their `Service`/`Person` types, actor IDs, actor-owned public keys,
individual inbox/outbox paths and the same Bridge shared inbox. The Service
publishes a `PropertyValue` attachment (`Bridge software` =
`discord-fediverse-bridge`) as an explicit Bridge protocol declaration. An
accepted subscription to that exact Service and target Local Group is required
both before and after the bounded, public-IP-validated actor fetch. Ambiguous
multiple Services on the same origin, unavailable/contradictory actors,
unsigned inner Person claims, revoked subscriptions and policy denial are
rejected without content effects. A verified *same-actor* accepted subscriber
keeps its prior path. CP21 completes durable pre-REST host reservation; CP23
will correct origin Service exclusion and immutable relay provenance.

### Local Group host reservation (CP21)

After each signed Create is admitted to one specific Local Group (CP19) and
its Person/Service subscription is verified (CP20), Python reserves a
`LocalCommunityThread` or `LocalCommunityMessage` keyed by
`(local_community_id, ap_object_id)`. The first record pins the original
`source_owner_actor_id`, `origin_subscriber_actor_id`, root and direct parent.
A single CAS attempt token transitions `reserved|failed` to `sending` **in
SQLite before** Discord `create_thread`/`send`; only the winner calls REST.
A confirmed response commits the Discord host surface with `delivered` in
the same transaction. A provably pre-send failure may be retried; an ambiguous
send or stale sending attempt is `unknown`, without automatic resubmission.
Two authorized local Groups may store the same AP object independently, while
a forged duplicate, different origin or foreign parent cannot reuse another
tenant's host or relay privileges. All duplicate deliveries recheck subscriber
ACL first. This uses the existing canonical host state fields, not a second
host-claim table or a restart recovery worker.

The generic `MessageMapping` is a single AP reference, **not** a local Group
placement lookup. `POST /internal/fedify/message-mappings/resolve` therefore
returns `409` rather than choosing its first stored Group when multiple local
host placements have the same object ID. A reply with explicit verified local
Group targeting uses its tenant-scoped canonical parent. The gateway may not
infer an absent local Group from a competing global mapping.

### Locally hosted Group mutation admission (CP22)

`post.updated|deleted` and `comment.updated|deleted` targeting a locally
hosted Bridge `Group` are distinct from updates distributed by a subscribed
remote Group. The gateway must provide the same trusted, server-selected
`verified_destination_group_actor_id` and outer
`verified_sender_actor_id` as for a local Create. A verified direct
`Update`/`Delete` must be signed by its `Person` owner; an `Announce` signed
by a Group does not independently authenticate its embedded mutation. The
original wire activity ID and verified source record remain unchanged.

Python first authenticates the Person through a current accepted same-actor
subscriber or the verified Bridge Service relationship, then checks the
**initial** canonical `(local_community_id, ap_object_id, kind,
source_owner_actor_id, origin_subscriber_actor_id)` under SQLite's writer lock.
The reply's persisted root and immediate parent must belong to that exact
Group; any ancestry supplied by a mutation must agree. A signed Delete with
an ID-only Tombstone may omit ancestry but cannot overwrite or infer it.
The original host surface must be delivered, the origin subscriber must still
be accepted, and current federation policy must permit the sender. Denied,
ambiguous and legacy NULL-owner rows produce no Discord edit/delete or relay.
The global `MessageMapping` is not a local Group mutation authorization source.

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

### Local Group Follow retry and Undo identity (CP18)

`local.follow_requested` and `local.unfollow_requested` retain their original
wire ActivityPub `delivery_id` and verified `Follow.id`. No `is_retry` field is
accepted from the gateway. After internal bearer/header checks, the Python HTTP
receipt boundary captures `is_retry=True` **only** for a previously failed or
deferred `local.follow_requested` with the same persisted Follow ID, before
resetting its receipt to `in_progress`. Other receipt categories retain their
normal deduplication behavior; receipt state does not prove the absence of a
later unsubscribe.

The local-community follower handler checks the accepted subscriber for that
same `(local_community_id, remote_actor_id)` under `BEGIN IMMEDIATE`. A retry
matching the current Follow ID may resend the original Accept to the saved
inbox. A retry of a superseded Follow or an absent accepted subscriber returns
`skipped/ignored_already_applied` without updating the subscriber or sending
Accept. Only a verified first-seen Follow ID may start/replace a generation.
For `Undo(Follow)`, a single SQL DELETE requires a non-empty exact current
`follow_activity_id` and accepted subscriber; older, missing and unrelated IDs
return `skipped/ignored_unknown_follow`. This inbound Service relationship is
separate from the outbound NodeBB Follow cancellation lifecycle.

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
| `/update` | `update_content` | Render immutable direct Group Update or retain legacy non-direct delivery | actor, community, object, kind, body, `renderOnly`, `softwareProfile`, `conversationRootObjectId` | Direct Group returns JSON without transport; other paths retain prior delivery |
| `/delete` | `delete_content` | Render immutable direct Group Delete or retain legacy non-direct delivery | actor, community, object, `renderOnly`, `softwareProfile`, `conversationRootObjectId` | Direct Group returns JSON without transport; other paths retain prior delivery |

### Durable remote community Create (CP15/CP16 boundary)

The Discord remote-community service derives one operation digest from the immutable
`(guild_id, forum_channel_id, Discord message_id, registered Person, canonical Group, kind)`.
Typed User Actor object/Create IDs and the original UTC Discord timestamp are sent
to `/render-community-create`. The gateway returns **only JSON**, without querying
remote actors or sending ActivityPub. It renders Note roots for persisted `nodebb`,
Page roots for confirmed `threadiverse`/`bridge`, and Note replies for each.
A NodeBB root uses its own canonical object ID as `context`; replies carry the
verified conversation root and immediate `inReplyTo`, not a guessed remote topic URL.
The private published-object resolver and User Actor outbox list expose
`original_object_type` and `original_object_context` read from the immutable
outbound Create source, so public GET and outbox cannot turn that NodeBB Note
into a Page after restart. A missing historical Create source keeps the legacy
Page/Note fallback; corrupted source identity fails closed. The NodeBB Update
render-only path explicitly adds the saved conversation root to its final signed
JSON, because the Fedify Note serializer does not retain constructor `context`.
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

### Direct remote Group Update/Delete (CP17)

Discord-origin Update/Delete first records durable desired content against the
original `PublishedActivityObject`, then reserves a stable revision in
`UserActorContentMutation.request_json`. Remote Group reservations copy the
**original Create** `UserActorOutboundSource.snapshot_json.directGroupRecipients`
(Group actor, inbox, Follow generation, guild/forum and software profile).
Missing or contradictory original recipient evidence is a local refusal: a new
Follow, a matching URL or an unrelated follower does not reconstruct it.
Local Bridge-hosted Groups and personal-feed mutations keep their existing
non-direct paths.

While the original direct Create endpoint remains `pending`, `failed`, `sending`,
`unknown` or otherwise unconfirmed, the mutation reservation is not claimed
for rendering. After the Create is confirmed `delivered`, Python invokes the
existing authenticated `/update` or `/delete` with `renderOnly=true` and the
saved software profile and conversation root. This path validates stable IDs
and constructs the canonical ActivityPub JSON **without** remote discovery or
`sendActivity`. NodeBB Update roots use Note with the original root context;
threadiverse/Bridge roots preserve Page; Delete retains the immutable object ID.
Python verifies the returned actor, ID, object, Group and kind before an atomic
`commit_canonical` creates a new ready source, carrying the exact original
`directGroupRecipients` and endpoint. On restart both the reservation and
source snapshot remain in SQLite.

The existing delivery worker rechecks that same Follow generation, channel
binding, Group/inbox policy and ordering at claim. Direct Group Delete cannot
bypass pending/failed/unknown Create or earlier mutation revisions. A dropped
Follow is not rescued by a User Actor follower at the same inbox. `unknown`
means the network may have succeeded and blocks blind resubmission; no remote
acceptance is claimed. This boundary does not alter follower-only delivery
ordering or automatic recovery policy.

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
