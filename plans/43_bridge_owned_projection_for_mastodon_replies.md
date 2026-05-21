# 43 — Bridge-Owned Projection Objects for Mastodon Replies

## Problem / Goal

Current behavior for Lemmy-origin posts in the bridge community:

```text
Lemmy Create(Page) in bridge community
→ gateway receives post.created
→ Discord receives the post
→ Python relays the post to Mastodon
→ Mastodon displays/imports the post
```

But when a Mastodon user writes the first reply to that relayed Lemmy-origin post, the reply bypasses the bridge:

```text
Mastodon reply Note
  inReplyTo = https://lemmy.nu31.space/post/250
  cc = [mastodon author followers, https://lemmy.nu31.space/u/admin]
  no bridge community / bridge actor / bridge inbox

→ Lemmy receives the reply
→ gateway does not receive Create(Note)
→ Discord does not receive the reply
```

Confirmed example:

```text
Mastodon status: https://mastodon.social/@nachitima/116613906276982637
AP object id: https://mastodon.social/ap/users/116015738644832902/statuses/116613906276982637
inReplyTo: https://lemmy.nu31.space/post/250
cc: mastodon followers + https://lemmy.nu31.space/u/admin
```

The bridge currently relays Lemmy-origin posts to Mastodon as an `Announce` of the original Lemmy `Create(Page)` / original Lemmy `Page`. Mastodon therefore treats the visible status as a Lemmy-origin object and addresses replies back to Lemmy, not to the bridge.

Goal: for Mastodon-facing relay of Lemmy-origin bridge-community posts, expose a **bridge-owned projection object** with a stable `bot-test.nachitima.com` object id and bridge-owned attribution, so future Mastodon replies use a bridge object as `inReplyTo` and reach the gateway inbox. The bridge will then translate those replies back to the original Lemmy post/comment for outbound Lemmy delivery.

This is generic bridge/projection behavior, not a Mastodon-specific hack. Mastodon is the currently observed recipient implementation, but the protocol problem is that replies follow the object identity and addressing of the object shown to the replying server.

## External References / Prior Art

- ActivityPub server-to-server delivery is recipient/inbox based; activities are posted to actors' inbox endpoints and delivery recipients are derived from ActivityPub addressing and linked actors.
  - https://www.w3.org/TR/activitypub/#delivery
- A similar problem exists in WordPress ActivityPub/Lemmy interop: comments left from Mastodon on content federated to Lemmy do not reach the Lemmy community.
  - https://github.com/Automattic/wordpress-activitypub/issues/1001
- Lemmy/Mastodon interop discussions note that Mastodon replies can federate back to Lemmy when replying to Lemmy `Page`/`Note`, but group/thread behavior and boosts are a separate compatibility surface.
  - https://socialhub.activitypub.rocks/t/help-improving-federation-between-lemmy-and-other-projects/2308
  - https://github.com/LemmyNet/lemmy/issues/2224
- There are reports from implementers that Mastodon replies may not be delivered to the expected origin inbox even when likes/deletes are delivered, which reinforces that reply delivery depends on follow/addressing/object shape.
  - https://github.com/mastodon/mastodon/discussions/34812

## Expected Behavior

For a Lemmy-origin post in a Discord-origin bridge community:

1. Gateway still receives the Lemmy `Create(Page)` and emits `post.created`.
2. Discord still receives the post.
3. Relay to Lemmy followers remains unchanged.
4. Relay to Mastodon-compatible followers no longer exposes the original Lemmy object as the replied-to object.
5. Python renders and stores a bridge-owned projection:

```text
original object id:
  https://lemmy.nu31.space/post/250

projection object id:
  https://bot-test.nachitima.com/communities/great_community/projections/<projection_id>

projection create id:
  https://bot-test.nachitima.com/communities/great_community/projections/<projection_id>/activity
```

6. Mastodon receives an ActivityPub activity whose visible object has:

```json
{
  "id": "https://bot-test.nachitima.com/communities/great_community/projections/<projection_id>",
  "type": "Page",
  "attributedTo": "https://bot-test.nachitima.com/communities/great_community",
  "audience": "https://bot-test.nachitima.com/communities/great_community",
  "to": ["https://www.w3.org/ns/activitystreams#Public"],
  "cc": ["https://bot-test.nachitima.com/communities/great_community/followers"],
  "name": "Test-19",
  "content": "<p>Hello from Lemmy!</p><p><a href=\"https://lemmy.nu31.space/post/250\">Original Lemmy post</a></p>",
  "source": {
    "content": "Hello from Lemmy!\n\nOriginal Lemmy post: https://lemmy.nu31.space/post/250",
    "mediaType": "text/markdown"
  },
  "url": "https://bot-test.nachitima.com/communities/great_community/projections/<projection_id>"
}
```

7. When a Mastodon user replies, the expected Mastodon AP object should become:

```json
{
  "type": "Note",
  "inReplyTo": "https://bot-test.nachitima.com/communities/great_community/projections/<projection_id>",
  "cc": [
    "https://mastodon.social/ap/users/.../followers",
    "https://bot-test.nachitima.com/communities/great_community"
  ]
}
```

Exact `cc` shape is implementation-dependent, but the bridge-owned projection id must appear in `inReplyTo`, and gateway must receive the `Create(Note)`.

8. Gateway resolves replies to projection objects:

```text
inReplyTo = bridge projection object id
→ lookup projection mapping
→ original_object_id = https://lemmy.nu31.space/post/250
→ community_actor_url = https://bot-test.nachitima.com/communities/great_community
→ emit comment.created with:
   post_ap_id = https://lemmy.nu31.space/post/250
   parent_ap_id = null for top-level comment
```

9. Python mirrors the Mastodon reply into Discord and persists `message_mappings` for the Mastodon comment.
10. Python relays that Mastodon-origin comment to Lemmy using the normalized Lemmy-compatible relay renderer from plan 42, with:

```text
Note.inReplyTo = original Lemmy post/comment id, not the projection id
```

## Non-Goals

- Do not implement comment sync/backfill from Lemmy as the primary fix.
- Do not rely on Lemmy re-federating Mastodon-origin replies back to the bridge.
- Do not change local Discord-origin publish behavior.
- Do not change Lemmy-target relay behavior except where it must translate projection replies back to original Lemmy object ids.
- Do not introduce full remote-user shadow actors in this plan. Shadow actors may become a later improvement if community-attributed projections are not sufficient for Mastodon reply delivery.

## Architecture

### Current relevant runtime path

```text
Lemmy Create(Page)
→ fedify-gateway/src/federation.ts
→ fedify-gateway/src/normalize.ts
→ Python /internal/activitypub/events
→ src/activitypub_handlers.py
→ src/local_communities/runtime.py
→ Discord mirror
→ src/local_communities/federation_fanout.py
→ src/local_communities/activitypub_renderers.py
→ gateway /send-local-community-relay
→ Mastodon inbox
```

The current Mastodon-target relay sends an `Announce` whose embedded object is still the original Lemmy object. That preserves remote identity, but it also causes replies to target Lemmy.

### New projection model

Add a projection layer for Mastodon-compatible relay targets:

```text
original Lemmy object
  ↓ projection mapping
bridge-owned projection object
  ↓ Mastodon reply targets projection object
inbound Mastodon Create(Note)
  ↓ gateway projection lookup
normalized comment.created using original Lemmy parent/post ids
```

Projection mapping is separate from `message_mappings` because it maps two ActivityPub object identities, not a platform message placement:

```text
bridge projection object id ↔ original remote object id
```

`message_mappings` still records Discord placement and dedup for actual mirrored posts/comments.

## Touched Files

- src/local_communities/activitypub_renderers.py
- src/local_communities/federation_fanout.py
- src/local_communities/runtime.py
- src/local_communities/delivery_mapping.py
- src/local_communities/inbound_mapping.py
- src/db.py
- src/activitypub_models.py
- fedify-gateway/src/server.ts
- fedify-gateway/src/published-objects.ts
- fedify-gateway/src/normalize.ts
- fedify-gateway/src/types.ts
- fedify-gateway/tests/verify-published-object-store.ts
- fedify-gateway/tests/verify-normalize-reply-chain.ts
- tests/behavior/test_local_community_remote_fanout_scenarios.py
- notes/known_issues.md

## New Files

Expected new module:

- src/local_communities/projection_mapping.py

Optional new tests if the existing behavior test file becomes too large:

- tests/behavior/test_local_community_projection_replies.py

## Data Model

Add a table to persist projection identity mapping:

```sql
CREATE TABLE local_community_projection_mappings (
    id INTEGER PRIMARY KEY,
    community_actor_url VARCHAR(512) NOT NULL,
    projection_object_id VARCHAR(512) NOT NULL UNIQUE,
    projection_activity_id VARCHAR(512) NOT NULL UNIQUE,
    original_object_id VARCHAR(512) NOT NULL,
    original_activity_id VARCHAR(512),
    object_kind VARCHAR(32) NOT NULL,
    source_actor_url VARCHAR(512) NOT NULL,
    created_at DATETIME NOT NULL
);

CREATE INDEX ix_local_projection_original_object
ON local_community_projection_mappings (original_object_id);

CREATE INDEX ix_local_projection_community_original
ON local_community_projection_mappings (community_actor_url, original_object_id);
```

`object_kind` values:

```text
post
comment
```

Initial implementation only needs `post` for the observed bug. The schema includes `comment` so the next step can project Lemmy comments consistently without another migration.

Projection id generation:

```text
projection_id = base64url(sha256(community_actor_url + "\n" + original_object_id))[0:24]
projection_object_id = {base_url}/communities/{slug}/projections/{projection_id}
projection_activity_id = {projection_object_id}/activity
```

The projection id must be deterministic. Replaying the same relay must not create a second projection id.

## Implementation Steps

### 1. Add projection mapping persistence

Create `src/local_communities/projection_mapping.py` with functions:

```python
def make_projection_id(community_actor_url: str, original_object_id: str) -> str:
    """Return a stable URL-safe id for a bridge-owned projection object."""


def build_projection_urls(base_url: str, community_slug: str, projection_id: str) -> ProjectionUrls:
    """Build stable object/activity URLs for a projected remote AP object."""


def get_or_create_projection_mapping(
    db: Session,
    *,
    community_actor_url: str,
    community_slug: str,
    base_url: str,
    original_object_id: str,
    original_activity_id: str | None,
    object_kind: Literal["post", "comment"],
    source_actor_url: str,
) -> ProjectionMapping:
    """Persist and return a stable mapping between original AP object and bridge projection."""
```

Important behavior:

```text
- Use deterministic projection id before insert.
- If projection_object_id already exists, return the existing row.
- If original_object_id already has a projection for the same community, return it.
- Do not create projections for local bridge objects; only remote Lemmy/threadiverse-origin objects relayed to Mastodon-compatible followers.
```

### 2. Store fetchable projection activity/object JSON

Use the existing published object store if it supports arbitrary object ids. If it does not, extend `fedify-gateway/src/published-objects.ts` / corresponding Python client so projection object ids can be served publicly.

For each projection, store both:

```text
projection_activity_id → Create(Page)
projection_object_id   → Page
```

Example stored projection `Create(Page)`:

```json
{
  "@context": "https://www.w3.org/ns/activitystreams",
  "id": "https://bot-test.nachitima.com/communities/great_community/projections/abc123/activity",
  "type": "Create",
  "actor": "https://bot-test.nachitima.com/communities/great_community",
  "to": ["https://www.w3.org/ns/activitystreams#Public"],
  "cc": ["https://bot-test.nachitima.com/communities/great_community/followers"],
  "audience": "https://bot-test.nachitima.com/communities/great_community",
  "object": {
    "@context": "https://www.w3.org/ns/activitystreams",
    "id": "https://bot-test.nachitima.com/communities/great_community/projections/abc123",
    "type": "Page",
    "attributedTo": "https://bot-test.nachitima.com/communities/great_community",
    "audience": "https://bot-test.nachitima.com/communities/great_community",
    "to": ["https://www.w3.org/ns/activitystreams#Public"],
    "cc": ["https://bot-test.nachitima.com/communities/great_community/followers"],
    "name": "Test-19",
    "content": "<p>Hello from Lemmy!</p><p>Original: <a href=\"https://lemmy.nu31.space/post/250\">https://lemmy.nu31.space/post/250</a></p>",
    "source": {
      "content": "Hello from Lemmy!\n\nOriginal: https://lemmy.nu31.space/post/250",
      "mediaType": "text/markdown"
    },
    "published": "2026-05-21T18:09:36.390765Z",
    "url": "https://bot-test.nachitima.com/communities/great_community/projections/abc123"
  }
}
```

Do not set `attributedTo` to the original Lemmy author for the first implementation. The observed Mastodon reply addressed the original `attributedTo` (`https://lemmy.nu31.space/u/admin`), so keeping the original author as `attributedTo` would likely preserve the bug. Preserve original authorship visibly in content/source/byline instead.

### 3. Serve projection URLs from the gateway

Add a gateway route for projection object/activity fetches:

```text
GET /communities/:slug/projections/:projectionId
GET /communities/:slug/projections/:projectionId/activity
```

Implementation options:

```text
Preferred: route reads from the published object store by full request URL.
Fallback: route resolves projection id through DB and renders the stored JSON.
```

Response requirements:

```text
Content-Type: application/activity+json
projection object URL returns Page/Note
projection activity URL returns Create(Page/Note)
404 for unknown projection id
```

### 4. Render projection payloads for Mastodon-compatible targets

Modify `src/local_communities/federation_fanout.py` so target-specific rendering can choose projection mode.

Current conceptual call:

```python
activity_json = render_local_community_relay_activity(source_activity_json=...)
```

New call shape:

```python
activity_json = render_local_community_relay_activity(
    source_activity_json=source.source_activity_json,
    normalized_event=event,
    community=community,
    follower=target_follower,
    delivery_profile=target_follower.delivery_profile,
    projection_mapping=projection_mapping_or_none,
)
```

Target rule:

```text
if source object is remote Lemmy/threadiverse Page
and target follower looks Mastodon-compatible
and source object is not already bridge-local
then render bridge-owned projection Create/Page or Announce(Create/Page)
else keep current behavior
```

Because the current project may not have a robust Mastodon delivery profile, use a conservative target classifier:

```python
def should_use_projection_for_target(follower: LocalCommunityFollower) -> bool:
    """Return true when relay should expose a bridge-owned object to improve reply routing."""
    return "mastodon" in follower.remote_actor_id or "mastodon" in follower.remote_inbox_url
```

This heuristic is acceptable only as a first step and must be isolated behind a named function so a real `delivery_profile = mastodon_compat` can replace it later. Do not scatter `mastodon.social` checks through renderers.

### 5. Decide direct Create vs Announce(Create)

Preferred first implementation: send a community `Announce` whose embedded object is the bridge-owned `Create(Page)`.

```json
{
  "@context": "https://www.w3.org/ns/activitystreams",
  "id": "https://bot-test.nachitima.com/communities/great_community/activities/announce/<id>",
  "type": "Announce",
  "actor": "https://bot-test.nachitima.com/communities/great_community",
  "to": ["https://www.w3.org/ns/activitystreams#Public"],
  "cc": ["https://bot-test.nachitima.com/communities/great_community/followers"],
  "object": {
    "id": "https://bot-test.nachitima.com/communities/great_community/projections/abc123/activity",
    "type": "Create",
    "actor": "https://bot-test.nachitima.com/communities/great_community",
    "object": {
      "id": "https://bot-test.nachitima.com/communities/great_community/projections/abc123",
      "type": "Page",
      "attributedTo": "https://bot-test.nachitima.com/communities/great_community"
    }
  }
}
```

Reason: current Mastodon import path already works with community `Announce(...)`; changing to direct `Create` may alter accepted/follower semantics more than necessary.

If live testing shows Mastodon still replies to the original Lemmy URL, stop and update the plan before switching to direct community `Create(Page)`. Do not silently change delivery shape beyond this plan.

### 6. Normalize inbound replies to projection objects

Modify `fedify-gateway/src/normalize.ts` community resolution / reply chain logic:

Current behavior handles:

```text
inReplyTo = local bridge post/comment object id
inReplyTo = mapped remote Lemmy comment object id
```

Add:

```text
inReplyTo = bridge projection object id
→ lookup local_community_projection_mappings.projection_object_id
→ community_actor_url from projection mapping
→ original_object_id for actual thread target
```

Normalization result for a Mastodon top-level reply to projected Lemmy post:

```json
{
  "event_type": "comment.created",
  "community_actor_id": "https://bot-test.nachitima.com/communities/great_community",
  "object": {
    "ap_id": "https://mastodon.social/ap/users/.../statuses/116613906276982637",
    "kind": "comment",
    "parent_ap_id": null,
    "post_ap_id": "https://lemmy.nu31.space/post/250",
    "post_lemmy_id": 250,
    "body_markdown": "test-1 from mastodon"
  }
}
```

For reply to projected remote comment later:

```text
projection object id maps to original comment id
→ parent_ap_id = original comment id
→ post_ap_id resolved from parent mapping as today
```

### 7. Translate projection parent ids during Lemmy relay

When Python relays a Mastodon-origin comment back to Lemmy, the outbound Lemmy-compatible Note must not use projection ids:

```text
Wrong:
  inReplyTo = https://bot-test.../projections/abc123

Right:
  inReplyTo = https://lemmy.nu31.space/post/250
```

The gateway normalization should already emit original Lemmy `post_ap_id` / `parent_ap_id`, so the existing renderer from plan 42 should work after projection-aware normalization. Add tests to lock this down.

### 8. Update known issues

Update `notes/known_issues.md`:

```text
Mastodon replies to Lemmy-origin posts in bridge community currently bypass bridge inbox because Mastodon replies to original Lemmy object ids. Planned fix: bridge-owned projection objects for Mastodon-facing relay so future replies target bot-test projection URLs.
```

Only mark fixed after live verification shows:

```text
Mastodon reply AP inReplyTo = https://bot-test.../projections/...
gateway receives Create(Note)
Discord receives reply
Lemmy receives relayed reply
```

## Tests

### Python behavior tests

Add to `tests/behavior/test_local_community_remote_fanout_scenarios.py` or new `tests/behavior/test_local_community_projection_replies.py`.

#### Test 1 — Mastodon-target relay uses projection for Lemmy-origin post

Given:

```text
source event: Lemmy Create(Page) for https://lemmy.nu31.space/post/250
community: https://bot-test.nachitima.com/communities/great_community
follower: Mastodon actor/inbox
```

When:

```text
LocalCommunityRuntime handles post.created and fanout runs
```

Assert:

```text
send_local_community_relay called for Mastodon follower
outbound activity object.object.id starts with https://bot-test.../communities/great_community/projections/
outbound embedded Page.attributedTo == bridge community actor
outbound embedded Page.content includes original Lemmy URL visibly
projection mapping row exists
published object store contains projection object and projection activity
```

#### Test 2 — Lemmy-target relay does not use projection

Given the same Lemmy-origin post and a Lemmy follower target.

Assert:

```text
outbound relay to Lemmy preserves existing Lemmy-compatible behavior
no projection payload is sent to Lemmy
```

#### Test 3 — Projection id is deterministic

Given the same `community_actor_url` and `original_object_id` twice.

Assert:

```text
same projection_object_id
only one local_community_projection_mappings row
```

#### Test 4 — Inbound reply to projection maps back to original Lemmy post

Given:

```text
projection_object_id -> https://lemmy.nu31.space/post/250
Mastodon Create(Note).object.inReplyTo = projection_object_id
```

When gateway normalization runs.

Assert normalized event:

```text
community_actor_id = bridge community
object.post_ap_id = https://lemmy.nu31.space/post/250
object.parent_ap_id = null
object.body_markdown = sanitized Mastodon body
```

#### Test 5 — Lemmy relay uses original object id, not projection id

Given normalized Mastodon reply from Test 4.

Assert outbound Lemmy relay:

```text
Note.inReplyTo = https://lemmy.nu31.space/post/250
Note.inReplyTo != projection_object_id
```

### Gateway tests

Add or update `fedify-gateway/tests/verify-normalize-reply-chain.ts`:

```text
projection parent id inReplyTo
→ lookup projection mapping
→ emits comment.created with original post_ap_id
```

Add or update `fedify-gateway/tests/verify-published-object-store.ts`:

```text
GET projection object URL returns ActivityPub Page
GET projection activity URL returns ActivityPub Create(Page)
unknown projection id returns 404
```

## Conflicts / Compatibility Risks

1. **Authorship display changes in Mastodon.**
   If projection `attributedTo` is bridge community, Mastodon may show the post as authored by the bridge community rather than by `admin@lemmy.nu31.space`. This is intentional for reply routing, but the content should clearly include original author/source link.

2. **Duplicate Mastodon statuses.**
   Existing deployments already sent Announce(original Lemmy post). After this change, the same Lemmy post may appear again as a bridge projection. This is acceptable during transition but should be noted in release notes. Do not try to rewrite old Mastodon objects.

3. **Mastodon may still reply to original URL if the UI chooses `url` instead of `id`.**
   Keep `url` equal to the projection URL. Include the original Lemmy URL only inside content/source, not as top-level `url`, to avoid Mastodon choosing the original as reply target.

4. **Lemmy may reject relayed Mastodon replies if inReplyTo points to projection id.**
   This must not happen. Projection-aware normalization must translate projection ids back to original Lemmy ids before Python relays to Lemmy.

5. **Delivery profile detection is incomplete.**
   The project currently may not persist a robust `delivery_profile`. Initial implementation can isolate a heuristic, but should not hard-code Mastodon logic across modules.

6. **Existing `message_mappings` uniqueness.**
   Do not insert projection object ids into `message_mappings.object_id` unless they correspond to an actual Discord message placement. Use `local_community_projection_mappings` for projection identity.

7. **Object fetchability is correctness-critical.**
   Mastodon often fetches object ids after receiving an activity. Projection object/activity URLs must be publicly fetchable with ActivityPub content type, or Mastodon import/reply behavior may be inconsistent.

8. **Plan 42 interaction.**
   The normalized Lemmy relay renderer from plan 42 must remain active for Mastodon-origin replies. This plan changes how replies get into the gateway; it does not replace the need to render Lemmy-compatible outbound Notes.

## Regression / Blind-Spot Analysis

- Discord-origin local posts must remain unchanged. They already have bridge-local object ids and replies route through gateway.
- Mastodon replies to mapped remote Lemmy comments currently work when the parent mapping exists. Projection support must not break this path.
- Lemmy-origin comments relayed to Mastodon may later need projections too. This plan starts with posts because the confirmed missing direct delivery happens on first reply to Lemmy-origin post.
- Existing retry rows may contain old Announce(original) payload assumptions. Rendering should happen at send time so new attempts use projection mode when applicable.
- If a Mastodon follower has already imported the old original-Lemmy status, future replies to that old status will still bypass the bridge. This plan only fixes newly relayed projection statuses.
- If Mastodon does not deliver replies to a Group actor unless it has accepted follows correctly, the issue may persist. Verify that the community actor has a valid inbox/sharedInbox and that Mastodon follower state is accepted.

## Live Verification Checklist

1. Publish a new Lemmy post into the bridge community.
2. Confirm gateway receives `Create(Page)` and Discord receives the post.
3. Confirm relay to Mastodon delivers a projection object, not the original Lemmy object.
4. Fetch the Mastodon-visible status and verify its AP object has:

```text
id = https://bot-test.../communities/great_community/projections/...
attributedTo = https://bot-test.../communities/great_community
url = projection URL, not original Lemmy URL
```

5. Reply from Mastodon.
6. Confirm the reply AP object has:

```text
inReplyTo = projection object URL
```

7. Confirm gateway log contains:

```text
POST /inbox from Mastodon
Create(Note)
Resolved comment community via projection-parent
Delivering event comment.created
```

8. Confirm Discord receives the reply.
9. Confirm Lemmy receives the relayed reply under the original Lemmy post.

## Open Questions

1. Should projection objects be represented as `Page` or `Note` for Mastodon compatibility?
   - Initial answer: use `Page` for projected posts to preserve current post semantics; switch only if Mastodon reply/import behavior requires `Note`.

2. Should the bridge use community actor attribution or local shadow actors for original Lemmy users?
   - Initial answer: use community actor attribution for the first implementation because it is simpler and most likely to route replies to the bridge. Shadow actors are a larger identity model and should be planned separately.

3. Should old already-relayed Lemmy posts be re-announced as projections?
   - Initial answer: no. Only new relays should use projections. Old holes can remain known behavior unless a migration/backfill plan is created.

4. Should projection mapping include comments now?
   - Initial answer: schema supports comments, but implementation starts with posts. Do not expand to projected comments unless a test requires it.
