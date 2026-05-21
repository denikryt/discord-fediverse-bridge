# 42 — Normalize Mastodon-Origin Reply Relay for Lemmy Targets

## Problem / Goal

Mastodon-origin replies now reach the bridge and mirror into Discord correctly:

```text
Mastodon Create(Note)
→ fedify-gateway normalizes comment.created
→ Python mirrors the comment into Discord
→ message_mappings is created
```

The remaining failure is outbound relay from the local community to Lemmy followers. The Python relay path currently takes `event.source_activity_json`, persists it as the relay source, and `src/local_communities/activitypub_renderers.py` wraps that source activity unchanged inside a community `Announce` for every delivery profile.

Current failing shape:

```text
Announce(
  Mastodon-shaped Create(
    Mastodon-shaped Note
  )
)
```

Lemmy rejects this with `400 Bad Request` because the embedded `Create(Note)` is not a Lemmy/threadiverse-compatible person-inbox activity. The observed rejected payload contains Mastodon-specific fields and shapes:

```text
Create.actor = embedded Person object, not actor IRI string
Note.interactionPolicy
Note.contentMap
Note.context / Note.conversation
Note.likes / Note.shares / Note.replies collections
Mastodon Mention tag objects/arrays
Note.to = "as:Public"
Create.to = "as:Public"
```

Goal: when relaying a non-threadiverse/Mastodon-shaped `Create(Note)` to a Lemmy/threadiverse follower, render a normalized Lemmy-compatible `Announce(Create(Note))` from the already-normalized bridge event data instead of preserving the raw Mastodon activity as-is.

This plan is only about create/comment relay. It must not change inbound Discord rendering, gateway signing, follow/unfollow, updates, or deletes.

## Expected Behavior

For a Mastodon-origin reply whose `ActivityPubEvent` has:

```text
event.event_type = comment.created
event.actor_id = https://mastodon.social/ap/users/...
event.object.ap_id = https://mastodon.social/ap/users/.../statuses/...
event.object.body_markdown = sanitized body text, for example "test-2 from mastodon"
event.object.parent_ap_id = known local or remote Lemmy parent
event.object.post_ap_id = root Lemmy/local post AP id
event.community_actor_id = local community actor
```

and for a target follower with `delivery_profile = threadiverse_group`, the relay delivery must contain:

```json
{
  "@context": "https://www.w3.org/ns/activitystreams",
  "id": "https://bridge.example/communities/hackers/activities/announce/<generated>",
  "type": "Announce",
  "actor": "https://bridge.example/communities/hackers",
  "to": ["https://www.w3.org/ns/activitystreams#Public"],
  "cc": ["https://bridge.example/communities/hackers/followers"],
  "object": {
    "@context": "https://www.w3.org/ns/activitystreams",
    "id": "https://mastodon.social/ap/users/.../statuses/.../activity",
    "type": "Create",
    "actor": "https://mastodon.social/ap/users/...",
    "audience": "https://bridge.example/communities/hackers",
    "to": ["https://www.w3.org/ns/activitystreams#Public"],
    "cc": ["https://bridge.example/communities/hackers", "https://mastodon.social/ap/users/..."],
    "object": {
      "@context": "https://www.w3.org/ns/activitystreams",
      "id": "https://mastodon.social/ap/users/.../statuses/...",
      "type": "Note",
      "attributedTo": "https://mastodon.social/ap/users/...",
      "audience": "https://bridge.example/communities/hackers",
      "to": ["https://www.w3.org/ns/activitystreams#Public"],
      "cc": ["https://bridge.example/communities/hackers", "https://mastodon.social/ap/users/..."],
      "content": "<p>test-2 from mastodon</p>",
      "mediaType": "text/html",
      "source": {
        "content": "test-2 from mastodon",
        "mediaType": "text/markdown"
      },
      "inReplyTo": "https://lemmy.nu31.space/comment/227",
      "published": "2026-05-21T01:55:50Z",
      "url": "https://mastodon.social/@nachitima/..."
    }
  }
}
```

The rendered Lemmy-target payload must not contain these Mastodon-only fields anywhere under the embedded `Create.object`:

```text
interactionPolicy
contentMap
context
conversation
likes
shares
replies
atomUri
inReplyToAtomUri
```

The embedded `Create.actor` and `Note.attributedTo` must be string IRIs. They must not be embedded actor objects.

Lemmy-origin relay must remain unchanged. If the inbound source is already threadiverse-shaped, the renderer must preserve the existing announce-as-source behavior.

## Architecture

Current runtime path in the codebase:

```text
fedify-gateway/src/federation.ts
  inbound direct Create(Note)
  → fedify-gateway/src/normalize.ts
  → emits ActivityPubEvent(comment.created)
  → event.source_activity_json keeps the original raw source activity

src/activitypub_handlers.py
  handle_comment_created
  → LocalCommunityRuntime.handle_inbound_comment

src/local_communities/runtime.py
  handle_inbound_comment
  → Discord send
  → database.create_local_community_message(...)
  → _persist_inbound_activitypub_message_mapping(...)
  → federation_fanout.relay_create(...)

src/local_communities/federation_fanout.py
  LocalCommunityFederationFanout.relay_create
  → _relay_to_targets
  → database.get_or_create_local_community_relay_source_activity(...)
  → database.create_missing_local_community_relay_deliveries(...)
  → render_local_community_relay_activity(...)
  → fedify_gateway.send_local_community_relay(...)

src/local_communities/activitypub_renderers.py
  render_local_community_relay_activity
  → currently calls _render_announce(source_activity_json, ...)
```

The fix belongs in `src/local_communities/activitypub_renderers.py`, with a small explicit call-site change in `src/local_communities/federation_fanout.py`.

The gateway must not be changed for this feature. It already signs and sends the provided JSON correctly. The Python renderer owns the outbound ActivityPub shape.

## Touched Files

- src/local_communities/activitypub_renderers.py
- src/local_communities/federation_fanout.py
- tests/behavior/test_local_community_remote_fanout_scenarios.py
- notes/known_issues.md

## New Files

- None expected.

## Implementation Details

### 1. Extend renderer input explicitly

Change the renderer function signature from:

```python
render_local_community_relay_activity(
    *,
    source_activity_json: dict,
    community_actor_url: str,
    community_slug: str,
    delivery_profile: str,
) -> dict
```

to:

```python
render_local_community_relay_activity(
    *,
    source_activity_json: dict,
    normalized_event: object,
    community_actor_url: str,
    community_slug: str,
    delivery_profile: str,
) -> dict
```

`normalized_event` is the existing `ActivityPubEvent` object already available inside `LocalCommunityFederationFanout._relay_to_targets(...)` as `event`.

Do not pass individual loose parameters unless the implementation becomes clearer. The event already contains the normalized bridge contract:

```text
event.actor_id
event.delivery_id
event.object.ap_id
event.object.parent_ap_id
event.object.body_markdown
event.object.published_at
event.object.url
event.object.kind
```

### 2. Update fanout call site

In `src/local_communities/federation_fanout.py`, update this block:

```python
activity_json = render_local_community_relay_activity(
    source_activity_json=source.source_activity_json,
    community_actor_url=getattr(local_community, "actor_url"),
    community_slug=getattr(local_community, "slug"),
    delivery_profile=row.delivery_profile,
)
```

to pass the normalized event:

```python
activity_json = render_local_community_relay_activity(
    source_activity_json=source.source_activity_json,
    normalized_event=event,
    community_actor_url=getattr(local_community, "actor_url"),
    community_slug=getattr(local_community, "slug"),
    delivery_profile=row.delivery_profile,
)
```

Add a short comment explaining why both `source_activity_json` and `normalized_event` are passed:

```text
source_activity_json preserves source identity; normalized_event provides sanitized bridge text and parent chain.
```

### 3. Add source-shape detection helpers

Add private helpers in `activitypub_renderers.py`:

```python
def _is_create_note(activity: dict) -> bool:
    """Return whether the source activity is a Create whose object is a Note."""


def _is_threadiverse_compatible_create_note(activity: dict) -> bool:
    """Return whether a Create(Note) can be preserved for threadiverse relay."""


def _needs_threadiverse_note_projection(activity: dict, normalized_event: object) -> bool:
    """Return whether a threadiverse target needs a normalized Note projection."""
```

Detection rules:

```text
Only consider projection when:
- delivery_profile == threadiverse_group
- normalized_event.event_type == comment.created
- normalized_event.object.kind == comment
- source_activity_json.type == Create
- source_activity_json.object.type == Note
```

Treat a source as non-threadiverse-shaped when any of these are true:

```text
source_activity_json["actor"] is a dict
source_activity_json["object"].get("interactionPolicy") is present
source_activity_json["object"].get("contentMap") is present
source_activity_json["object"].get("context") is present
source_activity_json["object"].get("conversation") is present
source_activity_json["object"].get("likes") / shares / replies is present
source_activity_json["object"].get("to") == "as:Public"
source_activity_json.get("to") == "as:Public"
```

Do not hard-code `mastodon.social`. The behavior is generic: non-threadiverse `Create(Note)` projection for threadiverse targets.

### 4. Keep existing preserve path unchanged

The main renderer branch should be:

```python
if delivery_profile == "threadiverse_group" and _needs_threadiverse_note_projection(source_activity_json, normalized_event):
    return _render_threadiverse_note_announce(
        source_activity_json=source_activity_json,
        normalized_event=normalized_event,
        community_actor_url=community_actor_url,
        community_slug=community_slug,
    )
return _render_announce(source_activity_json, community_actor_url, community_slug)
```

This preserves current behavior for:

```text
Lemmy-origin posts
Lemmy-origin comments
updates/deletes
mastodon_compat targets
generic_activitypub targets
unknown delivery profiles after fallback
```

### 5. Implement `_render_threadiverse_note_announce(...)`

Add a private renderer:

```python
def _render_threadiverse_note_announce(
    *,
    source_activity_json: dict,
    normalized_event: object,
    community_actor_url: str,
    community_slug: str,
) -> dict:
    """Render a Lemmy-compatible community Announce for a non-threadiverse Create(Note)."""
```

Use these values:

```text
source_create_id = source_activity_json["id"] if string else normalized_event.delivery_id
source_note = source_activity_json["object"]
note_id = normalized_event.object.ap_id
actor_id = normalized_event.actor_id
parent_id = normalized_event.object.parent_ap_id
body = normalized_event.object.body_markdown or ""
published = normalized_event.object.published_at ISO string
url = normalized_event.object.url
followers_url = f"{community_actor_url.rstrip('/')}/followers"
```

The outer Announce must still use the generated community announce id, same style as `_render_announce(...)`:

```python
announce_id = f"{community_actor_url.rstrip('/')}/activities/announce/{timestamp}-{uuid}"
```

The embedded `Create` should be newly constructed, not copied and deleted from the Mastodon source. That avoids missing a Mastodon-only field.

Embedded Create shape:

```python
create = {
    "@context": "https://www.w3.org/ns/activitystreams",
    "id": source_create_id,
    "type": "Create",
    "actor": actor_id,
    "audience": community_actor_url,
    "to": [PUBLIC_COLLECTION],
    "cc": [community_actor_url, actor_id],
    "object": note,
}
```

Embedded Note shape:

```python
note = {
    "@context": "https://www.w3.org/ns/activitystreams",
    "id": note_id,
    "type": "Note",
    "attributedTo": actor_id,
    "audience": community_actor_url,
    "to": [PUBLIC_COLLECTION],
    "cc": [community_actor_url, actor_id],
    "content": _markdown_text_to_html_paragraphs(body),
    "mediaType": "text/html",
    "source": {
        "content": body,
        "mediaType": "text/markdown",
    },
    "published": _isoformat_activitypub(normalized_event.object.published_at),
    "url": normalized_event.object.url,
}
```

Add `inReplyTo` only when `parent_id` is truthy:

```python
if parent_id:
    note["inReplyTo"] = parent_id
```

Do not copy any of these from the Mastodon source:

```text
interactionPolicy
contentMap
context
conversation
atomUri
inReplyToAtomUri
likes
shares
replies
tag
attachment
summary
sensitive
```

A later patch can add attachments/tags if needed. This patch should keep the payload minimal and parseable.

### 6. Implement body HTML helper

Add a small helper in `activitypub_renderers.py`:

```python
def _markdown_text_to_html_paragraphs(text: str) -> str:
    """Render normalized bridge text as conservative ActivityPub HTML."""
```

Behavior:

```text
- HTML-escape the input with html.escape
- preserve line breaks by converting consecutive paragraphs or lines
- for this patch, simple `<p>escaped text with <br /> for line breaks</p>` is enough
- empty text becomes `<p></p>` or a safe empty string only if existing project renderers allow it
```

Concrete implementation acceptable for this patch:

```python
from html import escape

escaped = escape(text.strip())
return f"<p>{escaped.replace(chr(10), '<br />')}</p>"
```

Do not reintroduce Mastodon leading mentions. Use `normalized_event.object.body_markdown`, which was already sanitized by gateway normalization.

### 7. ISO timestamp helper

If `published_at` is a `datetime`, render it as ActivityPub-compatible UTC/ISO:

```python
def _isoformat_activitypub(value: object) -> str:
    """Return an ActivityPub timestamp string from a normalized event value."""
```

Implementation constraints:

```text
- If value has isoformat(), use it.
- Replace +00:00 with Z for UTC if present.
- If value is already str, return it.
```

Do not introduce a new dependency for timestamp formatting.

## Implementation Steps

1. Add a failing behavior test in `tests/behavior/test_local_community_remote_fanout_scenarios.py`.

   New test name:

   ```text
   test_mastodon_shaped_comment_relay_to_lemmy_gets_threadiverse_payload
   ```

   Test setup:

   ```text
   database, runtime = _runtime(tmp_path)
   local_community = _local_community(database)
   _add_followers(database, local_community)
   runtime.bot = build_bot(...)
   ````

   Seed a mapped root thread/post so `handle_inbound_comment` can place the comment:

   ```python
   database.create_local_community_thread(
       local_community_id=local_community.id,
       discord_thread_id=200,
       discord_starter_message_id=300,
       ap_activity_id="https://lemmy.example/activities/create/post/243",
       ap_object_id="https://lemmy.example/post/243",
       direction="ap_to_discord",
       origin_kind="remote_follower",
   )
   ```

   Configure fake bot thread send to return a created message. Follow existing inbound comment tests for fake Discord helpers.

   Build an `ActivityPubEvent` with:

   ```text
   event_type = comment.created
   actor_id = https://mastodon.example/ap/users/alice
   delivery_id = https://mastodon.example/ap/users/alice/statuses/1/activity
   object.ap_id = https://mastodon.example/ap/users/alice/statuses/1
   object.body_markdown = test-2 from mastodon
   object.parent_ap_id = https://lemmy.example/comment/227
   object.post_ap_id = https://lemmy.example/post/243
   object.post_lemmy_id = 243
   object.lemmy_id = 0
   source_activity_json = Mastodon-shaped Create(Note)
   ```

   Add followers where the origin actor is skipped and one Lemmy target receives the relay. Existing `_add_followers` can be adapted or a local test-specific target can be inserted directly.

   Fake `send_local_community_relay` to return success so the runtime completes and delivered rows can be asserted.

2. In that test, capture the outbound request:

   ```python
   request = runtime.fedify_gateway.send_local_community_relay.await_args.kwargs
   delivery = request["deliveries"][0]
   activity = delivery.activity_json
   create = activity["object"]
   note = create["object"]
   ```

   Assert exact important fields:

   ```python
   assert activity["type"] == "Announce"
   assert activity["actor"] == local_community.actor_url
   assert create["type"] == "Create"
   assert create["id"] == "https://mastodon.example/ap/users/alice/statuses/1/activity"
   assert create["actor"] == "https://mastodon.example/ap/users/alice"
   assert isinstance(create["actor"], str)
   assert create["audience"] == local_community.actor_url
   assert create["to"] == [PUBLIC_COLLECTION]
   assert local_community.actor_url in create["cc"]
   assert note["id"] == "https://mastodon.example/ap/users/alice/statuses/1"
   assert note["type"] == "Note"
   assert note["attributedTo"] == "https://mastodon.example/ap/users/alice"
   assert note["inReplyTo"] == "https://lemmy.example/comment/227"
   assert note["audience"] == local_community.actor_url
   assert note["content"] == "<p>test-2 from mastodon</p>"
   assert note["source"] == {"content": "test-2 from mastodon", "mediaType": "text/markdown"}
   assert note["mediaType"] == "text/html"
   ```

   Assert forbidden fields are absent:

   ```python
   for key in ["interactionPolicy", "contentMap", "context", "conversation", "likes", "shares", "replies", "atomUri", "inReplyToAtomUri"]:
       assert key not in note
   assert isinstance(create["actor"], str)
   ```

3. Add or keep a preservation regression test for Lemmy-origin relay.

   Existing `test_accepted_remote_post_relays_to_other_followers_only` covers post preservation. Add a comment-specific preservation test if none exists:

   ```text
   test_lemmy_shaped_comment_relay_preserves_source_activity
   ```

   It should build a Lemmy-shaped `Create(Note)` with:

   ```text
   actor = string
   object.audience = local community actor
   object.to/cc include threadiverse addressing
   object.source.mediaType = text/markdown
   ```

   Assert the outbound embedded `Create` is still the same source shape, not newly projected.

4. Update `activitypub_renderers.py` with the helpers and projection branch described above.

   Keep comments/docstrings because project rules require them and because this is compatibility-sensitive logic.

5. Update `federation_fanout.py` call site to pass `normalized_event=event`.

6. Update `notes/known_issues.md` only after code/tests reflect the new status:

   ```text
   - Keep Mastodon-origin reply relay to Lemmy as open until live Lemmy returns 200.
   - Add a note that the code now renders a normalized threadiverse payload if the patch is implemented.
   ```

7. Run:

   ```bash
   pytest tests/behavior/test_local_community_remote_fanout_scenarios.py
   pytest
   cd fedify-gateway && npm run check
   ```

   Gateway tests are not required unless TypeScript contracts change. This plan should not change gateway code.

## Tests

Required new regression test:

```text
Given a Mastodon-shaped direct Create(Note) reply to a mapped Lemmy/local-community thread
And a Lemmy/threadiverse follower target
When LocalCommunityRuntime.handle_inbound_comment mirrors the reply and relays it
Then the outbound activity is Announce(normalized Create(Note))
And the embedded Create/Note are Lemmy-compatible
And Mastodon-only fields are absent
And the normalized body text is used
```

Required preservation test:

```text
Given a Lemmy-shaped Create(Note)
When the relay renderer targets a threadiverse follower
Then the existing preserve-and-announce behavior remains unchanged
```

Required no-transport-change check:

```text
send_local_community_relay is still called with the same signing_actor_url and delivery list shape.
Only delivery.activity_json changes for non-threadiverse Create(Note) → threadiverse target.
```

## Conflicts / Compatibility Risks

- `delivery_profile` is currently coarse. Followers often default to `threadiverse_group`. This means the normalized payload may also be sent to non-Lemmy followers that are stored with the default profile. The implementation must be conservative and ActivityPub-valid, but live verification should focus on Lemmy.
- Reusing the original Mastodon `Create.id` with a normalized body may be semantically acceptable but is still a projection. If Lemmy rejects it after parse succeeds, the next option is to mint a local normalized Create id while keeping `Note.id` as the Mastodon object id. Do not implement that fallback in this patch unless tests or live data prove it necessary.
- Lemmy may validate remote actor fetch/signature expectations for `Create.actor`. This patch only fixes the parse-shape problem. If Lemmy later rejects the actor or object semantically, record a separate issue.
- Do not make the local community the author of the Note. The community announces; the Mastodon actor remains the author.
- Do not normalize Lemmy-origin activities unnecessarily. They already relay to Mastodon successfully and must remain stable.
- Update/delete relay for Mastodon-origin comments may still be Mastodon-shaped. This patch covers create relay only.
- Existing failed relay rows may be retried and rendered through the new code because rendering happens at send time from stored source activity rows. This is useful, but could cause old failed Mastodon relay rows to send normalized payloads after the patch.

## Regression / Blind-Spot Analysis

- If a Mastodon reply has attachments, this minimal projection drops them. That is acceptable for text-comment compatibility and should be tracked separately if attachment relay becomes required.
- If a Mastodon reply contains multiple mentions, the projection uses the sanitized bridge body and does not preserve Mastodon mention tags. Discord rendering already stripped routing mentions. This may omit explicit mentions on Lemmy, but avoids parse failures.
- If `event.object.body_markdown` is empty after sanitization, the generated `<p></p>` may still be accepted but should be watched in live logs.
- If a target profile later becomes `mastodon_compat`, this plan intentionally keeps preserving the source activity for that profile. Do not apply threadiverse projection to all targets globally.
- The renderer cannot know whether a `threadiverse_group` target is exactly Lemmy or another implementation. The generated payload should remain generic ActivityPub + Lemmy-compatible, not Lemmy-domain-specific.

## Open Questions

None for this plan.
