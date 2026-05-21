# Unfollow Workflow Diagnostics

Status: investigation note. Do not treat this as an implementation plan yet.

## Problem / goal

The bridge appears to send an unfollow request, but the gateway may continue receiving ActivityPub POST requests from the remote side afterward. The current evidence is not enough to prove whether unfollow failed, whether old delivery jobs are retrying, or whether the incoming requests are unrelated to the follow relationship.

The goal is to capture the exact `Follow`, exact `Undo(Follow)`, remote inbox response, local DB state, and post-unfollow inbound traffic so the actual failure mode can be identified before changing behavior.

## Facts to establish

1. Which `Follow` activity the bridge originally sent.
2. Which `Undo(Follow)` activity the bridge actually sends.
3. Whether the remote inbox accepts, rejects, or ignores the `Undo(Follow)` delivery.
4. Which ActivityPub requests still arrive at the gateway after unfollow.
5. Whether those post-unfollow requests are new follower deliveries, retries of old activities, deliveries from another actor, or deliveries unrelated to the follow relationship.

## Current suspicious area

`fedify-gateway/src/federation-outbound.ts` builds an `Undo` containing an embedded `Follow` object:

```ts
const undo = new Undo({
  id: new URL(`${config.fedifyOrigin}activities/undo/...`),
  actor: actorUri,
  object: new Follow({
    id: new URL(followActivityId),
    actor: actorUri,
    object: new URL(communityId),
  }),
});
```

This may be valid ActivityPub behavior, because `Undo(Follow)` should undo the side effects of a previous `Follow`. However, specific implementations may match strictly by original follow id, actor, object, canonical actor URL, or even by expecting `Undo.object` to be an IRI instead of an embedded `Follow` object.

## Diagnostic logging needed

### Inbound gateway audit

Add temporary or env-gated JSONL audit logging around the gateway inbox handler before normalization/dispatch.

Each incoming POST should record:

```text
timestamp
method/path
remote ip / x-forwarded-for
user-agent
signature keyId
activity.type
activity.id
activity.actor
activity.object.type
activity.object.id
activity.object.actor
activity.object.object
to/cc/audience
```

Suggested file:

```text
logs/activitypub-inbox-audit.jsonl
```

This distinguishes:

```text
- new remote community delivery after unfollow
- retries of old activities
- traffic from another actor or another community
- replies/likes/announces that are not caused by the follow relationship
```

### Outbound Follow and Undo(Follow) audit

Log the serialized outbound ActivityPub payloads, not only summary fields.

For original `Follow`, record:

```text
id
actor
object
target inbox
timestamp
serialized JSON-LD payload
```

For `Undo(Follow)`, record:

```text
id
actor
object.type
object.id
object.actor
object.object
to/cc if present
target inbox
timestamp
serialized JSON-LD payload
```

The important comparison:

```text
original Follow.id == Undo.object.id
original Follow.actor == Undo.object.actor
original Follow.object == Undo.object.object
```

If any of those do not match, the remote side may not recognize the unfollow.

### Remote inbox HTTP response

Do not rely only on a local “sendActivity succeeded” log. Capture the actual remote inbox response:

```text
HTTP status
response headers
response body prefix
final inbox URL
network error if any
```

Interpretation:

```text
2xx: delivery was accepted, but semantic unfollow may still have failed
4xx/5xx: remote rejected the request; inspect payload, signature, actor, object, and inbox
network error: delivery did not happen
```

Possible implementation options:

```text
- wrap gateway global fetch/undici during diagnostics and log outbound POST responses;
- or add a dedicated diagnostic sender that sends the same Undo(Follow) and exposes response details.
```

The fetch wrapper is probably the fastest way to see what Fedify receives from the remote inbox without reimplementing HTTP signatures.

## Database checks

Before and after unfollow, compare subscription state and bridge actor follow state.

```sql
.headers on
.mode column

SELECT id, discord_channel_id, lemmy_community_actor_id, follow_activity_id, status
FROM channel_community_subscriptions
WHERE lemmy_community_actor_id = '<community actor>';

SELECT id, community_actor_id, follow_activity_id, status, created_at, updated_at
FROM bridge_actor_follows
WHERE community_actor_id = '<community actor>';
```

Also compare both stored follow ids directly:

```sql
SELECT
  s.lemmy_community_actor_id,
  s.follow_activity_id AS subscription_follow_id,
  b.follow_activity_id AS bridge_follow_id,
  s.status AS subscription_status,
  b.status AS bridge_follow_status
FROM channel_community_subscriptions s
LEFT JOIN bridge_actor_follows b
  ON b.community_actor_id = s.lemmy_community_actor_id
WHERE s.lemmy_community_actor_id = '<community actor>';
```

Important suspicion: `fedify-gateway/src/unfollow-community-cli.ts` may read `follow_activity_id` from `channel_community_subscriptions`, while the normal unsubscribe workflow may use `bridge_actor_follows`. If those ids diverge, the bridge could send `Undo(Follow)` for the wrong original follow activity.

## Controlled test flow

Use one known remote community actor with an active follow.

### 1. Baseline

Run gateway with inbound audit enabled before unfollow. Capture at least one incoming event or wait long enough to establish current traffic.

Record:

```text
from actor
activity type
activity id
object id
community actor
user-agent
```

### 2. Trigger unfollow through the normal workflow

Prefer the production path first, for example Discord unsubscribe or the bridge’s normal internal unsubscribe flow. Avoid starting with the manual CLI unless the CLI itself is the suspected path.

If calling gateway directly, use the `follow_activity_id` from `bridge_actor_follows` first:

```bash
curl -sS -X POST 'http://127.0.0.1:3000/unfollow-community' \
  -H "Authorization: Bearer $PYTHON_BRIDGE_SHARED_SECRET" \
  -H 'Content-Type: application/json' \
  --data-binary '{
    "communityActorUrl": "<community actor>",
    "followActivityId": "<bridge_actor_follows.follow_activity_id>"
  }' | jq
```

### 3. Capture unfollow result

Immediately collect:

```text
local /unfollow-community response
serialized outbound Undo(Follow)
remote inbox HTTP status/headers/body prefix
DB state after unfollow
```

### 4. Capture post-unfollow inbound traffic

After unfollow, create or wait for a new remote community activity and inspect gateway audit.

Classify traffic as:

```text
old retry:
  same activity.id was seen before unfollow

new follower delivery:
  new activity.id, remote community actor, object.created after unfollow

wrong actor/community:
  activity actor or community does not match the unfollowed community

unrelated delivery:
  request is a reply, like, announce, or other activity not caused by community follower delivery
```

## If remote-side logs are available

For a Lemmy instance under our control, inspect remote-side logs or DB to answer:

```text
Did Lemmy receive the Undo?
Which actor did Lemmy see?
Which object id did Lemmy see?
Did Lemmy find an existing follow row?
Did Lemmy remove the follower row?
Which HTTP status did Lemmy return?
```

If the remote instance is not controlled, rely on the gateway outbound HTTP response and subsequent observed delivery behavior.

## Vendor behavior comparison

If possible, capture how Lemmy itself formats an unfollow when a Lemmy user unsubscribes from a community. Compare whether Lemmy sends:

```json
{
  "type": "Undo",
  "actor": "...",
  "object": "https://.../activities/follow/..."
}
```

or:

```json
{
  "type": "Undo",
  "actor": "...",
  "object": {
    "type": "Follow",
    "id": "https://.../activities/follow/...",
    "actor": "...",
    "object": "..."
  }
}
```

If Lemmy expects `Undo.object` as an IRI but the bridge sends an embedded `Follow`, run an A/B test on a controlled follow/unfollow pair.

## Minimum evidence bundle before fixing

Collect these artifacts before changing the workflow:

```text
1. DB snapshot before unfollow:
   channel_community_subscriptions + bridge_actor_follows

2. Original Follow summary:
   id, actor, object, inbox, timestamp

3. Outbound Undo(Follow) JSON:
   complete serialized activity

4. Remote inbox response:
   status, headers, body/error

5. Inbound audit after unfollow:
   POSTs with actor/type/id/object/user-agent
```

## Most likely causes to check first

1. `Undo(Follow)` uses the wrong `followActivityId`, especially if `channel_community_subscriptions.follow_activity_id` differs from `bridge_actor_follows.follow_activity_id`.
2. Remote implementation expects `Undo.object` as an IRI to the old `Follow`, not an embedded `Follow` object.
3. Stored `communityActorUrl` differs from the remote actor’s canonical `id`, causing actor/object mismatch.
4. Gateway logs delivery success without exposing the remote inbox HTTP status/body.
5. Incoming requests after unfollow are retries, traffic from another actor, or activities unrelated to follower delivery.

## Next practical step

Implement a small diagnostic patch only:

```text
- inbound AP audit JSONL;
- outbound Follow/Undo JSON dump;
- outbound remote inbox response logging;
- SQL helper or CLI command comparing subscription follow id vs bridge_actor_follows follow id.
```

Then run one controlled unfollow against one community. Do not change unfollow semantics until this evidence exists.
