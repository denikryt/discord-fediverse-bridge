/** Runtime invariant checks for NodeBB and unchanged Lemmy/Bridge Create shapes. */
import assert from "node:assert/strict";
import { renderCommunityCreate } from "../src/outbound/community-create-renderer.js";
import type { GatewayConfig } from "../src/config.js";

const origin = "https://bridge.example";
const operationId = "f".repeat(64);
const actor = `${origin}/actors/users/alice`;
const group = "https://forum.example/category/42";
const base = {
  actorUsername: "alice", communityActorUrl: group,
  operationId, kind: "post" as const, title: "My topic",
  objectId: `${actor}/objects/posts/${operationId}`,
  activityId: `${actor}/activities/create/posts/${operationId}`,
  bodyMarkdown: "Hello **world**", publishedAt: "2026-11-10T12:13:14.000Z",
  inReplyToObjectId: null, conversationRootObjectId: null,
};
const config = { fedifyOrigin: origin } as GatewayConfig;
for (const profile of ["nodebb", "threadiverse", "bridge"] as const) {
  const result = renderCommunityCreate(config, { ...base, softwareProfile: profile });
  const activity = result.activityJson;
  const object = activity.object as Record<string, unknown>;
  assert.equal(result.activityId, base.activityId);
  assert.equal(activity.actor, actor);
  assert.equal(object.type, profile === "nodebb" ? "Note" : "Page");
  assert.equal(object.audience, group);
  assert.equal(object.attributedTo, actor);
  assert.deepEqual(activity.to, ["https://www.w3.org/ns/activitystreams#Public", group]);
  assert.equal(object.context, profile === "nodebb" ? base.objectId : undefined);
}
const comment = renderCommunityCreate(config, {
  ...base, kind: "comment", softwareProfile: "nodebb", title: null,
  objectId: `${actor}/objects/comments/${operationId}`,
  activityId: `${actor}/activities/create/comments/${operationId}`,
  inReplyToObjectId: `${origin}/actors/users/alice/objects/comments/parent`,
  conversationRootObjectId: base.objectId,
});
assert.equal((comment.activityJson.object as Record<string, unknown>).context, base.objectId);
assert.equal((comment.activityJson.object as Record<string, unknown>).inReplyTo,
  `${origin}/actors/users/alice/objects/comments/parent`);
assert.throws(() => renderCommunityCreate(config, { ...base, softwareProfile: "unknown" as "nodebb" }));
assert.throws(() => renderCommunityCreate(config, { ...base, softwareProfile: "nodebb", objectId: `${origin}/wrong` }));
assert.throws(() => renderCommunityCreate(config, { ...base, softwareProfile: "nodebb", publishedAt: "not-a-date" }));
assert.throws(() => renderCommunityCreate(config, {
  ...base, kind: "comment", softwareProfile: "nodebb",
  objectId: `${actor}/objects/comments/${operationId}`,
  activityId: `${actor}/activities/create/comments/${operationId}`,
}));
console.log("community Create renderer: verified profiles, fixed IDs, parents, and rejects OK");
