# 40 — Support Direct Note Replies to Local Parents

## Problem / Goal

Mastodon sends replies as direct `Create(Note)` activities to the gateway inbox. The gateway accepts the request with `202`, but the reply is not delivered to Python and no `message_mappings` row is created.

Observed payload shape:

- `type = Create`
- `object.type = Note`
- `object.inReplyTo = https://bot-test.nachitima.com/users/choikak2/comment/...`
- `object.to = Public`
- `object.cc = [mastodon followers, https://bot-test.nachitima.com/actors/choikak2]`
- no local community actor appears in `audience`, `to`, or `cc`

The existing gateway normalizer expects Lemmy-shaped comments: community is present in the object addressing, and comment ids contain `/comment/<number>`. Mastodon direct replies violate both assumptions. The goal is to support the generic protocol case: a remote direct `Create(Note)` reply to a known local object.

## Expected Behavior

When a direct `Create(Note)` arrives and `Note.inReplyTo` points to a known local post/comment:

- the gateway resolves the parent object from local stored mappings/object rows;
- the gateway derives `community_actor_id` from the parent mapping/object context;
- the gateway emits `comment.created` to Python;
- Python creates the Discord reply under the mapped parent;
- a `message_mappings` row is created for the remote reply object;
- Lemmy-shaped comment handling remains unchanged.

When a direct `Create(Note)` has no community in addressing and `inReplyTo` does not point to a known local object, the gateway must not guess a community. It should log an explicit debug/error reason and avoid emitting a malformed event.

## Architecture

The fix belongs in the gateway normalization layer, not in Python event handling.

Current direct create path:

```text
fedify-gateway/src/federation.ts
  .on(Create)
    → normalizeCreateActivity(activity)
      → normalizeCommentActivity(activity, note)
        → resolveReplyChainContext(inReplyTo)
        → resolveCommunityActorId(note)
        → parseRequiredLemmyNumericId(note.id, "comment")
```

Required behavior:

```text
normalizeCommentActivity
  → resolve reply chain from Note.inReplyTo
  → resolve community using explicit addressing first
  → if addressing has no community, resolve community from the local parent
  → parse Lemmy numeric id when present
  → use 0 for non-Lemmy remote comment ids
  → emit comment.created
```

Use an explicit model, not a vendor-specific branch:

```text
Community resolution source:
- addressing: community found in audience/to/cc
- local-parent: community derived from a known parent mapping/object
```

This should be treated as `direct-note-reply-to-local-parent`, not `mastodonPath`. Mastodon is the observed producer, but the protocol shape is generic.

## Touched Files

- fedify-gateway/src/normalize.ts
- fedify-gateway/src/federation.ts
- fedify-gateway/src/db.ts
- fedify-gateway/tests/verify-normalize-reply-chain.ts
- fedify-gateway/tests/verify-published-object-store.ts
- fedify-gateway/tests/verify-python-contract.ts

## New Files

None expected.

## Implementation Steps

1. Add a failing regression test for a direct `Create(Note)` reply to a stored local comment.
   - Fixture should mimic the observed Mastodon-shaped payload.
   - The `Note` must not include a local community in `audience`, `to`, or `cc`.
   - `Note.inReplyTo` must point to a local stored comment/post.
   - Expected event: `comment.created` with `community_actor_id` from the parent and `lemmy_id = 0`.

2. Extend the DB read side if necessary.
   - Current published-object lookup returns `kind`, `objectId`, and `inReplyToObjectId`.
   - Community fallback also needs `communityActorUrl` for the known parent.
   - Prefer extending the existing internal row returned by `loadStoredActivityObject` instead of adding a second DB query.

3. Refactor community resolution for comments into an explicit helper.
   - First try existing addressing resolution.
   - If it fails, check whether `inReplyTo` resolved to a local parent with a known `communityActorUrl`.
   - Return a structured result with source metadata for debug logging.
   - Do not silently pick actor ids or arbitrary `cc` entries as communities.

4. Replace strict comment numeric-id parsing with a safe helper for inbound comments.
   - Lemmy `/comment/<number>` ids should still parse to the real number.
   - Non-Lemmy ids, including Mastodon `/statuses/<id>`, should produce `0`.
   - Keep strict parsing for paths where Lemmy ids are still contractually required.

5. Add explicit error logging around direct `Create` normalization/delivery in `federation.ts`.
   - Current direct Create failures can disappear after `202` with no actionable log.
   - The handler should log normalization failures with activity id and object id, then avoid throwing through the Fedify listener.
   - Do not emit partial events.

6. Preserve existing reply-chain behavior.
   - Reply-to-local-post and reply-to-local-comment chains must still resolve `post_ap_id`, `parent_ap_id`, and `post_lemmy_id` correctly.
   - Existing Lemmy direct and Announce-wrapped comments must continue to use community addressing normally.

7. Run targeted gateway checks and full Python tests.
   - `npm run check`
   - `verify-normalize-reply-chain`
   - `verify-published-object-store`
   - `verify-python-contract`
   - `pytest`

## Tests

Required tests:

- direct `Create(Note)` reply to a local comment, no community in addressing, community derived from parent mapping/object;
- direct `Create(Note)` reply to a local post, no community in addressing, community derived from parent mapping/object;
- non-Lemmy comment object id produces `lemmy_id = 0`;
- existing Lemmy-shaped direct comment still keeps parsed numeric `lemmy_id`;
- existing Announce-wrapped Lemmy comment normalization still passes;
- direct Create normalization failure logs a clear reason and does not emit malformed events.

Prefer scenario-style gateway verification scripts over isolated mock-only tests. DB-backed tests are preferred because the bug depends on stored local parent context.

## Conflicts / Compatibility Risks

- Python `ActivityPubObject.lemmy_id` is required. Using `0` for non-Lemmy remote comments preserves the current schema but must be treated as a sentinel.
- Deduplication uses `delivery_id`, `source_activity_id`, and `object.ap_id`; adding support for Mastodon ids must not create duplicate Discord messages when the same reply is redelivered.
- Community derivation from parent must only apply when `inReplyTo` is a known local object. Otherwise unrelated direct replies could be routed into the wrong community.
- Existing Lemmy handling must continue to prefer explicit ActivityPub addressing because Lemmy includes the community actor in `audience`/`to`/`cc`.
- Fetching remote parents is already supported for reply-chain walking, but community fallback must not infer community from arbitrary remote parents.
- Debug logging must not leak large raw payloads outside `LOG_LEVEL=debug`.

## Regression / Blind-Spot Analysis

- Direct `Create(Page)` from Lemmy is already working; changes to `normalizeCreateActivity` must not affect post creation.
- Announce-wrapped `Create(Note)` from Lemmy may use the JSON normalizer, so both typed and JSON paths need equivalent behavior or an explicit decision that only the typed direct path is in scope.
- Replies to a local comment require walking to the root post. The fallback must not set `post_ap_id` to the comment itself.
- Replies to stale/deleted local parents may resolve from `published_activity_objects` but fail later in Python if the Discord mapping is gone. This should be logged and handled by existing Python behavior.
- Mastodon may send mentions in HTML content. This plan does not sanitize or rewrite content; existing markdown/body conversion behavior remains in force.
- This plan does not address incoming Mastodon edits/deletes for these replies. Those should be verified later through `Update(Note)` and `Delete` handling.

## Open Questions

- Should non-Lemmy ids use `lemmy_id = 0` permanently, or should the Python schema later rename this field to a generic `remote_numeric_id`/nullable id? For this fix, keep `0` to avoid a cross-language contract migration.
- Should `message_mappings` be exposed to the gateway directly for parent community lookup, or should the fallback rely only on `published_activity_objects`? The current plan prefers existing published-object storage if it contains enough community context.
