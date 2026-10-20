/** Verify personal-feed conversations personal-feed Create/Update/Delete normalization and routing identity. */

import assert from "node:assert/strict";

import {
  normalizeCreateActivityFromJson,
  normalizeDeleteActivityFromJson,
  normalizeUpdateActivityFromJson,
} from "../src/normalize.js";

const ROOT = "https://bridge.example/actors/users/alice/objects/posts/root";
const PARENT = "https://remote.example/comments/parent";
const OBJECT = "https://remote.example/comments/child";
const ACTOR = "https://remote.example/users/bob";

const lookup = {
  async loadConversationTargetByObjectId(objectId: string): Promise<unknown> {
    if (objectId === ROOT || objectId === PARENT || objectId === OBJECT) {
      return {
        publicationContext: "personal_feed",
        rootObjectId: ROOT,
        actorId: objectId === OBJECT ? ACTOR : "https://bridge.example/actors/users/alice",
      };
    }
    return null;
  },
  async loadMessageMappingByObjectId(objectId: string): Promise<unknown> {
    if (objectId === ROOT || objectId === PARENT || objectId === OBJECT) {
      return {
        publicationContext: "personal_feed",
        objectId,
        conversationRootObjectId: ROOT,
        actorUrl: objectId === OBJECT ? ACTOR : "https://bridge.example/actors/users/alice",
      };
    }
    return null;
  },
  async loadPublishedActivityObjectByObjectId(objectId: string): Promise<unknown> {
    if (objectId === ROOT) {
      return {
        publicationContext: "personal_feed",
        objectId: ROOT,
        conversationRootObjectId: ROOT,
        actorUrl: "https://bridge.example/actors/users/alice",
      };
    }
    return null;
  },
};

async function main(): Promise<void> {
  const create = await normalizeCreateActivityFromJson({
    type: "Create",
    id: "https://remote.example/activities/create/1",
    actor: ACTOR,
    object: {
      type: "Note",
      id: OBJECT,
      attributedTo: ACTOR,
      inReplyTo: ROOT,
      source: { mediaType: "text/markdown", content: "reply" },
      published: "2026-06-17T12:00:00Z",
    },
  }, { pythonBridgeClient: lookup as never });
  assert.ok(create);
  assert.deepEqual(create.target, { kind: "personal_feed", root_object_id: ROOT });
  assert.equal(create.community_actor_id, null);
  assert.equal(create.object.attributed_to_actor_id, ACTOR);

  const update = await normalizeUpdateActivityFromJson({
    type: "Update",
    id: "https://remote.example/activities/update/1",
    actor: ACTOR,
    object: {
      type: "Note",
      id: OBJECT,
      attributedTo: ACTOR,
      inReplyTo: PARENT,
      source: { mediaType: "text/markdown", content: "edited" },
      updated: "2026-06-17T12:01:00Z",
    },
  }, { pythonBridgeClient: lookup as never });
  assert.ok(update);
  assert.equal(update.event_type, "comment.updated");
  assert.deepEqual(update.target, { kind: "personal_feed", root_object_id: ROOT });
  assert.equal(update.object.attributed_to_actor_id, ACTOR);

  const deleted = await normalizeDeleteActivityFromJson({
    type: "Delete",
    id: "https://remote.example/activities/delete/1",
    actor: ACTOR,
    object: OBJECT,
  }, { pythonBridgeClient: lookup as never });
  assert.ok(deleted);
  assert.equal(deleted.event_type, "comment.deleted");
  assert.deepEqual(deleted.target, { kind: "personal_feed", root_object_id: ROOT });
  assert.equal(deleted.object.attributed_to_actor_id, ACTOR);

  const wrongActor = await normalizeDeleteActivityFromJson({
    type: "Delete",
    id: "https://remote.example/activities/delete/forged",
    actor: "https://remote.example/users/mallory",
    object: OBJECT,
  }, { pythonBridgeClient: lookup as never });
  assert.ok(wrongActor);
  // The gateway preserves the sender and stored owner separately so Python can
  // atomically reject the mismatch before mutating canonical state.
  assert.equal(wrongActor.actor_id, "https://remote.example/users/mallory");
  assert.equal(wrongActor.object.attributed_to_actor_id, ACTOR);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
