import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { verifyMutationIdentity, updateContent, deleteContent } from "../src/federation-outbound.js";
import type { GatewayConfig } from "../src/config.js";
import type { UpdateContentRequest, DeleteContentRequest } from "../src/types.js";

const config: GatewayConfig = {
  actorIdentifier: "bridge", actorName: "Bridge", actorSummary: "Bridge",
  fedifyOrigin: "https://bridge.example/", port: 3000,
  pythonBridgeInternalUrl: "http://127.0.0.1:8080", pythonBridgeSharedSecret: "secret", logLevel: "info",
};

function stableJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${stableJson(record[key])}`).join(",")}}`;
}

function buildRequest(kind: "content_update" | "content_delete", targetInboxUrls: string[] | null = []) {
  const base = {
    actorUsername: "alice",
    communityActorUrl: "https://remote.example/c/group",
    apObjectId: "https://bridge.example/actors/users/alice/objects/posts/1",
    targetInboxUrls,
    mutationTimestamp: "2026-06-16T10:00:00+00:00",
    expectedRevision: 1,
    nextRevision: 2,
  };
  const requestShape = {
    actorUsername: base.actorUsername,
    communityActorUrl: base.communityActorUrl,
    apObjectId: base.apObjectId,
    kind: kind === "content_update" ? "post" : null,
    title: kind === "content_update" ? "Topic" : null,
    bodyMarkdown: kind === "content_update" ? "new" : null,
    inReplyToObjectId: null,
    targetInboxUrls: base.targetInboxUrls,
    mutationTimestamp: base.mutationTimestamp,
  };
  const identity = stableJson({objectId: base.apObjectId, kind, expectedRevision: 1, nextRevision: 2, request: requestShape});
  const operationId = createHash("sha256").update(identity).digest("hex");
  const activityId = `https://bridge.example/actors/users/alice/activities/${kind === "content_delete" ? "delete" : "update"}/posts/${operationId}`;
  return kind === "content_update"
    ? {...base, kind: "post" as const, title: "Topic", bodyMarkdown: "new", inReplyToObjectId: null, operationId, activityId}
    : {...base, operationId, activityId};
}

for (const kind of ["content_update", "content_delete"] as const) {
  const request = buildRequest(kind);
  verifyMutationIdentity(config, request as never, kind);
  assert.throws(() => verifyMutationIdentity(config, {...request, activityId: request.activityId + "-tampered"} as never, kind), /mutation activity identity mismatch/);
  const changedInput = kind === "content_update"
    ? {...request, bodyMarkdown: "different"}
    : {...request, targetInboxUrls: ["https://other.example/inbox"]};
  assert.throws(
    () => verifyMutationIdentity(config, changedInput as never, kind),
    /mutation operation identity mismatch/,
  );
  // Byte-equivalent retries remain valid across gateway process restarts because
  // verification derives identity entirely from immutable request inputs.
  verifyMutationIdentity(config, JSON.parse(JSON.stringify(request)) as never, kind);
}
// A durable remote Group mutation is rendered but never sent by the gateway.
// The original Group/inbox/Follow is authorized later by the Python delivery worker.
const noNetwork = { createContext: () => { throw new Error("render-only invoked Fedify network"); } };
const root = "https://bridge.example/actors/users/alice/objects/posts/1";
const updateRequest = {
  ...buildRequest("content_update", null), publicationContext: "community" as const,
  renderOnly: true, softwareProfile: "nodebb" as const,
  conversationRootObjectId: root,
} as UpdateContentRequest;
const updated = await updateContent(noNetwork as never, config, updateRequest);
assert.equal(updated.activityId, updateRequest.activityId);
assert.equal(updated.objectId, root);
assert.equal((updated.activityJson.object as Record<string, unknown>).type, "Note");
assert.equal((updated.activityJson.object as Record<string, unknown>).context, root);
assert.equal((updated.activityJson.object as Record<string, unknown>).audience, updateRequest.communityActorUrl);

const deleteRequest = {
  ...buildRequest("content_delete", null), publicationContext: "community" as const,
  renderOnly: true, softwareProfile: "nodebb" as const,
  conversationRootObjectId: root,
} as DeleteContentRequest;
const deleted = await deleteContent(noNetwork as never, config, deleteRequest);
assert.equal(deleted.activityJson.object, root);
assert.equal(deleted.activityJson.id, deleteRequest.activityId);
await assert.rejects(
  () => deleteContent(noNetwork as never, config,
    { ...deleteRequest, softwareProfile: null }),
  /verified Create provenance/,
);
await assert.rejects(
  () => updateContent(noNetwork as never, config,
    { ...updateRequest, targetInboxUrls: ["https://wrong.example/inbox"] }),
  /mutation operation identity mismatch|verified Create provenance/,
);

console.log("verify:content-mutation-identity passed");
