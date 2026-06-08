/**
 * Scenario tests for normalizeUpdateActivityFromJson and normalizeDeleteActivityFromJson.
 *
 * These normalizers handle the unwrapped inner record from Announce(Update(...))
 * and Announce(Delete(...)) payloads that Lemmy sends to community inboxes.
 *
 * System state: no DB, no network — normalizers accept raw JSON records.
 *
 * Tested actions:
 *   1. Update(Note)  → comment.updated, ap_id from Note.id
 *   2. Update(Page)  → post.updated, ap_id from Page.id
 *   3. Delete with plain-string object URL (primary Lemmy 0.19 path) → correct event + community from audience
 *   4. Delete for post URL  → post.deleted
 *   5. Delete with legacy { id: "..." } object (fallback) → post.deleted
 *   6. Update with unknown nested type → null (guard against unrecognized objects)
 *   7. Update with no object field → null (guard against malformed records)
 */

import assert from "node:assert/strict";

import {
  normalizeUpdateActivityFromJson,
  normalizeDeleteActivityFromJson,
} from "../src/normalize.js";

const LEMMY_ORIGIN = "https://lemmy.example";
const COMMUNITY_URL = `${LEMMY_ORIGIN}/c/testcommunity`;
const EMPTY_LOOKUP_CLIENT = {
  async loadMessageMappingByObjectId(): Promise<null> { return null; },
  async loadPublishedActivityObjectByObjectId(): Promise<null> { return null; },
};

async function main(): Promise<void> {
  await testNormalizeUpdateComment();
  await testNormalizeUpdatePost();
  await testNormalizeDeleteComment();
  await testNormalizeDeletePost();
  await testNormalizeDeleteByObjectFallback();
  await testNormalizeUpdateUnknownObjectType();
  await testNormalizeUpdateMissingObject();
  console.log("normalize update/delete tests passed");
}

/**
 * Action: Update wrapping a Note (comment).
 * Expected: event_type = "comment.updated", ap_id = the Note id.
 */
async function testNormalizeUpdateComment(): Promise<void> {
  const activity = {
    type: "Update",
    id: `${LEMMY_ORIGIN}/activities/update/comment-1`,
    actor: `${LEMMY_ORIGIN}/u/admin`,
    audience: COMMUNITY_URL,
    object: {
      type: "Note",
      id: `${LEMMY_ORIGIN}/comment/125`,
      attributedTo: `${LEMMY_ORIGIN}/u/admin`,
      to: [COMMUNITY_URL],
      audience: COMMUNITY_URL,
      inReplyTo: `${LEMMY_ORIGIN}/post/35`,
      source: { content: "Updated comment text", mediaType: "text/markdown" },
      published: "2026-05-11T10:00:00Z",
    },
  };

  const event = await normalizeUpdateActivityFromJson(activity, { pythonBridgeClient: EMPTY_LOOKUP_CLIENT });

  assert.ok(event, "normalizeUpdateActivityFromJson must return an event for Update(Note)");
  assert.equal(event.event_type, "comment.updated");
  assert.equal(event.object.ap_id, `${LEMMY_ORIGIN}/comment/125`);
  assert.equal(event.object.kind, "comment");
  assert.equal(event.object.body_markdown, "Updated comment text");
}

/**
 * Action: Update wrapping a Page (post).
 * Expected: event_type = "post.updated", ap_id = the Page id.
 */
async function testNormalizeUpdatePost(): Promise<void> {
  const activity = {
    type: "Update",
    id: `${LEMMY_ORIGIN}/activities/update/post-1`,
    actor: `${LEMMY_ORIGIN}/u/admin`,
    audience: COMMUNITY_URL,
    object: {
      type: "Page",
      id: `${LEMMY_ORIGIN}/post/35`,
      attributedTo: `${LEMMY_ORIGIN}/u/admin`,
      to: [COMMUNITY_URL],
      audience: COMMUNITY_URL,
      name: "Updated post title",
      source: { content: "Updated post body", mediaType: "text/markdown" },
      published: "2026-05-11T10:00:00Z",
    },
  };

  const event = await normalizeUpdateActivityFromJson(activity, { pythonBridgeClient: EMPTY_LOOKUP_CLIENT });

  assert.ok(event, "normalizeUpdateActivityFromJson must return an event for Update(Page)");
  assert.equal(event.event_type, "post.updated");
  assert.equal(event.object.ap_id, `${LEMMY_ORIGIN}/post/35`);
  assert.equal(event.object.kind, "post");
  assert.equal(event.object.body_markdown, "Updated post body");
}

/**
 * Action: Delete whose object is a plain string URL (primary Lemmy 0.19 format).
 * community_actor_id must be taken from the Delete record's audience field.
 * Expected: event_type = "comment.deleted", ap_id = the comment URL.
 */
async function testNormalizeDeleteComment(): Promise<void> {
  const activity = {
    type: "Delete",
    id: `${LEMMY_ORIGIN}/activities/delete/comment-1`,
    actor: `${LEMMY_ORIGIN}/u/admin`,
    audience: COMMUNITY_URL,
    cc: [COMMUNITY_URL],
    to: ["https://www.w3.org/ns/activitystreams#Public"],
    object: `${LEMMY_ORIGIN}/comment/125`,
  };

  const event = await normalizeDeleteActivityFromJson(activity);

  assert.ok(event, "normalizeDeleteActivityFromJson must return an event for Delete(string URL comment)");
  assert.equal(event.event_type, "comment.deleted");
  assert.equal(event.object.ap_id, `${LEMMY_ORIGIN}/comment/125`);
  assert.equal(event.object.kind, "comment");
  assert.equal(event.community_actor_id, COMMUNITY_URL);
}

/**
 * Action: Delete for a post URL.
 * Expected: event_type = "post.deleted", ap_id = the post URL.
 */
async function testNormalizeDeletePost(): Promise<void> {
  const activity = {
    type: "Delete",
    id: `${LEMMY_ORIGIN}/activities/delete/post-1`,
    actor: `${LEMMY_ORIGIN}/u/admin`,
    audience: COMMUNITY_URL,
    cc: [COMMUNITY_URL],
    to: ["https://www.w3.org/ns/activitystreams#Public"],
    object: `${LEMMY_ORIGIN}/post/35`,
  };

  const event = await normalizeDeleteActivityFromJson(activity);

  assert.ok(event, "normalizeDeleteActivityFromJson must return an event for Delete(string URL post)");
  assert.equal(event.event_type, "post.deleted");
  assert.equal(event.object.ap_id, `${LEMMY_ORIGIN}/post/35`);
  assert.equal(event.object.kind, "post");
  assert.equal(event.community_actor_id, COMMUNITY_URL);
}

/**
 * Action: Delete with legacy object format — { id: "..." } record instead of plain string.
 * Expected: fallback path resolves ap_id from object.id, still produces post.deleted.
 */
async function testNormalizeDeleteByObjectFallback(): Promise<void> {
  const activity = {
    type: "Delete",
    id: `${LEMMY_ORIGIN}/activities/delete/post-legacy`,
    actor: `${LEMMY_ORIGIN}/u/admin`,
    audience: COMMUNITY_URL,
    object: { id: `${LEMMY_ORIGIN}/post/35` },
  };

  const event = await normalizeDeleteActivityFromJson(activity);

  assert.ok(event, "normalizeDeleteActivityFromJson must handle legacy object-record format");
  assert.equal(event.event_type, "post.deleted");
  assert.equal(event.object.ap_id, `${LEMMY_ORIGIN}/post/35`);
}

/**
 * Action: Update wrapping an object with an unrecognized type.
 * Expected: returns null — guard against forwarding unknown object shapes.
 */
async function testNormalizeUpdateUnknownObjectType(): Promise<void> {
  const activity = {
    type: "Update",
    id: `${LEMMY_ORIGIN}/activities/update/unknown`,
    actor: `${LEMMY_ORIGIN}/u/admin`,
    object: {
      type: "Unknown",
      id: `${LEMMY_ORIGIN}/something/1`,
    },
  };

  const event = await normalizeUpdateActivityFromJson(activity, { pythonBridgeClient: EMPTY_LOOKUP_CLIENT });

  assert.equal(event, null, "normalizeUpdateActivityFromJson must return null for unknown nested type");
}

/**
 * Action: Update record with no object field at all.
 * Expected: returns null — guard against malformed/incomplete payloads.
 */
async function testNormalizeUpdateMissingObject(): Promise<void> {
  const activity = {
    type: "Update",
    id: `${LEMMY_ORIGIN}/activities/update/broken`,
    actor: `${LEMMY_ORIGIN}/u/admin`,
  };

  const event = await normalizeUpdateActivityFromJson(activity, { pythonBridgeClient: EMPTY_LOOKUP_CLIENT });

  assert.equal(event, null, "normalizeUpdateActivityFromJson must return null when object field is missing");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
