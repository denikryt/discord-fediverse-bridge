/** Local Community mutations render canonical authored JSON before durable dispatch. */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { updateContent, deleteContent } from "../src/federation-outbound.js";
import type { GatewayConfig } from "../src/config.js";
import { installSignedJsonSocketFixture } from "./support/signed-json-socket-fixture.js";

const origin = "https://discord-bridge.example.com";
const community = `${origin}/actors/communities/hackers`;
const actor = `${origin}/actors/users/alice`;
const objectId = `${actor}/objects/comments/1`;
const config: GatewayConfig = {
  actorIdentifier: "bridge", actorName: "Bridge", actorSummary: "Bridge",
  pythonBridgeInternalUrl: "http://unavailable.invalid", pythonBridgeSharedSecret: "secret",
  fedifyOrigin: origin, port: 3000, logLevel: "info",
};

/** Stable identity contains authored content and revisions, never recipients. */
function stableJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record).sort().map(key => `${JSON.stringify(key)}:${stableJson(record[key])}`).join(",")}}`;
}

/** Build the exact mutation receipt Python commits before any delivery worker. */
function requestFor(kind: "content_update" | "content_delete") {
  const update = kind === "content_update";
  const requestShape = {
    actorUsername: "alice", publicationContext: "community", communityActorUrl: community,
    conversationRootObjectId: null, apObjectId: objectId,
    kind: update ? "comment" : null, title: null,
    bodyMarkdown: update ? "Edited from Discord" : null,
    inReplyToObjectId: update ? `${actor}/objects/posts/1` : null,
    mutationTimestamp: "2026-10-25T10:00:00Z",
  };
  const operationId = createHash("sha256").update(stableJson({
    objectId, kind, expectedRevision: 1, nextRevision: 2, request: requestShape,
  })).digest("hex");
  const base = {
    actorUsername: "alice", communityActorUrl: community, apObjectId: objectId,
    mutationTimestamp: requestShape.mutationTimestamp,
    expectedRevision: 1, nextRevision: 2, operationId,
    activityId: `${actor}/activities/${update ? "update" : "delete"}/comments/${operationId}`,
  };
  return {base, update: {...base, kind: "comment" as const, title: null,
    bodyMarkdown: "Edited from Discord", inReplyToObjectId: requestShape.inReplyToObjectId}};
}

// Neither signed delivery nor Python recipient enumeration is permitted here.
const restore = installSignedJsonSocketFixture(() => {
  throw new Error("rendering must not dispatch a pre-commit mutation");
});
try {
  const update = requestFor("content_update");
  const rendered = await updateContent({} as never, config, update.update);
  assert.equal(rendered.activityId, update.base.activityId);
  assert.equal(rendered.objectId, objectId);
  assert.equal(rendered.activityJson.type, "Update");
  assert.equal(rendered.activityJson.actor, actor);
  const note = rendered.activityJson.object as Record<string, unknown>;
  assert.equal(note.id, objectId);
  assert.equal(note.type, "Note");
  assert.equal(note.attributedTo, actor);
  assert.equal(note.audience, community);
  assert.equal(note.inReplyTo, `${actor}/objects/posts/1`);
  assert.deepEqual((await updateContent({} as never, config, update.update)).activityJson,
                   rendered.activityJson);

  const deletion = requestFor("content_delete");
  const deleted = await deleteContent({} as never, config, deletion.base);
  assert.equal(deleted.activityId, deletion.base.activityId);
  assert.equal(deleted.activityJson.type, "Delete");
  assert.equal(deleted.activityJson.actor, actor);
  assert.equal(deleted.activityJson.object, objectId);
  assert.deepEqual((await deleteContent({} as never, config, deletion.base)).activityJson,
                   deleted.activityJson);
  console.log("verify:local-community-update-delete passed");
} finally {
  restore();
}
