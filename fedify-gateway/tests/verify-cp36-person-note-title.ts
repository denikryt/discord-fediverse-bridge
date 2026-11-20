import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { updateContent } from "../src/federation-outbound.js";
import type { GatewayConfig } from "../src/config.js";
import type { UpdateContentRequest } from "../src/types.js";

const config: GatewayConfig = {
  actorIdentifier: "bridge", actorName: "Bridge", actorSummary: "Bridge",
  fedifyOrigin: "https://bridge.example/", port: 3000,
  pythonBridgeInternalUrl: "http://127.0.0.1:8080",
  pythonBridgeSharedSecret: "secret", logLevel: "info",
};
const forbiddenNetwork = { createContext: () => { throw new Error("unexpected network"); } };
const base: UpdateContentRequest = {
  actorUsername: "alice", publicationContext: "personal_feed",
  communityActorUrl: null, kind: "post", title: null,
  apObjectId: "https://bridge.example/actors/users/alice/objects/posts/1",
  bodyMarkdown: "Правка 🦊", mutationTimestamp: "2026-11-20T00:00:00Z",
  directPersonActorIds: ["https://peer.example/users/bob"],
};
for (const publicationContext of ["personal_feed", "remote_person_feed"] as const) {
  const result = await updateContent(forbiddenNetwork as never, config, {...base, publicationContext});
  const obj = result.activityJson.object as Record<string, unknown>;
  assert.equal(obj.type, "Note");
  assert.equal(Object.hasOwn(obj, "name"), false, "title-less Person Update must not invent a title");
  assert.equal(obj.id, base.apObjectId);
  assert.equal(obj.attributedTo, "https://bridge.example/actors/users/alice");
  assert.ok(JSON.stringify(result.activityJson).includes("peer.example/users/bob"));
  assert.ok(JSON.stringify(obj).includes("Правка 🦊"));
  const named = await updateContent(forbiddenNetwork as never, config, {...base, publicationContext, title: "Named post"});
  assert.equal((named.activityJson.object as Record<string, unknown>).name, "Named post");
}
/** Match production's stable identity hash for a persisted Community revision. */
function stableJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${stableJson(record[key])}`).join(",")}}`;
}
const communityBase = {
  ...base, publicationContext: "community" as const,
  communityActorUrl: "https://remote.example/c/group",
  directPersonActorIds: [], softwareProfile: "nodebb" as const,
  renderOnly: true, conversationRootObjectId: base.apObjectId,
  expectedRevision: 1, nextRevision: 2,
};
const identity = stableJson({
  objectId: communityBase.apObjectId, kind: "content_update",
  expectedRevision: 1, nextRevision: 2,
  request: {
    actorUsername: communityBase.actorUsername, publicationContext: "community",
    communityActorUrl: communityBase.communityActorUrl,
    conversationRootObjectId: communityBase.conversationRootObjectId,
    apObjectId: communityBase.apObjectId, kind: communityBase.kind,
    title: communityBase.title, bodyMarkdown: communityBase.bodyMarkdown,
    inReplyToObjectId: null, targetInboxUrls: null,
    mutationTimestamp: communityBase.mutationTimestamp,
  },
});
const operationId = createHash("sha256").update(identity).digest("hex");
const communityNote = await updateContent(forbiddenNetwork as never, config, {
  ...communityBase, operationId,
  activityId: `https://bridge.example/actors/users/alice/activities/update/posts/${operationId}`,
});
assert.equal((communityNote.activityJson.object as Record<string, unknown>).name, "Untitled Discord Post");
await assert.rejects(() => updateContent(forbiddenNetwork as never, config, {
  ...base, communityActorUrl: "https://remote.example/c/group",
}), /personal-feed mutation cannot carry communityActorUrl/);
console.log("verify:cp36-person-note-title passed");
