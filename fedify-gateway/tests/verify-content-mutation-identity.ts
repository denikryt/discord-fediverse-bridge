import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { Temporal } from "@js-temporal/polyfill";
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

/** Fixture identities are always present, even though production API fields
 * are optional. Keep the update-only fields narrowed for Unicode hashing.
 */
type UpdateFixture = UpdateContentRequest & {
  kind: "post"; title: string; bodyMarkdown: string; inReplyToObjectId: null;
  operationId: string; activityId: string;
};
type DeleteFixture = DeleteContentRequest & { operationId: string; activityId: string };
function buildRequest(kind: "content_update", targetInboxUrls?: string[] | null): UpdateFixture;
function buildRequest(kind: "content_delete", targetInboxUrls?: string[] | null): DeleteFixture;
function buildRequest(kind: "content_update" | "content_delete", targetInboxUrls?: string[] | null): UpdateFixture | DeleteFixture;
function buildRequest(kind: "content_update" | "content_delete", targetInboxUrls: string[] | null = []): UpdateFixture | DeleteFixture {
  const base = {
    actorUsername: "alice",
    communityActorUrl: "https://remote.example/c/group",
    publicationContext: "community" as const,
    apObjectId: "https://bridge.example/actors/users/alice/objects/posts/1",
    conversationRootObjectId: "https://bridge.example/actors/users/alice/objects/posts/1",
    targetInboxUrls,
    mutationTimestamp: "2026-06-16T10:00:00+00:00",
    expectedRevision: 1,
    nextRevision: 2,
  };
  const requestShape = {
    actorUsername: base.actorUsername,
    communityActorUrl: base.communityActorUrl,
    publicationContext: base.publicationContext,
    conversationRootObjectId: base.conversationRootObjectId,
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

// Newly persisted Python edits must supply an absolute instant. The gateway
// intentionally rejects offset-free historical requests rather than guessing.
assert.equal(Temporal.Instant.from("2026-11-16T01:00:00+00:00").toString(), "2026-11-16T01:00:00Z");
assert.throws(() => Temporal.Instant.from("2026-11-16T01:00:00"), /offset|instant|ISO/i);

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
// JavaScript hashes UTF-8 text directly. Python's persisted request must use
// identical bytes for Unicode edits, not ASCII-escaped surrogate sequences.
const unicodeRequest = buildRequest("content_update");
const unicodeShape = {
  actorUsername: unicodeRequest.actorUsername,
  communityActorUrl: unicodeRequest.communityActorUrl,
  publicationContext: unicodeRequest.publicationContext,
  conversationRootObjectId: unicodeRequest.conversationRootObjectId,
  apObjectId: unicodeRequest.apObjectId,
  kind: unicodeRequest.kind,
  title: unicodeRequest.title,
  bodyMarkdown: "Правка 🦊",
  inReplyToObjectId: unicodeRequest.inReplyToObjectId,
  targetInboxUrls: unicodeRequest.targetInboxUrls,
  mutationTimestamp: unicodeRequest.mutationTimestamp,
};
const unicodeId = createHash("sha256").update(stableJson({
  objectId: unicodeRequest.apObjectId, kind: "content_update",
  expectedRevision: unicodeRequest.expectedRevision,
  nextRevision: unicodeRequest.nextRevision, request: unicodeShape,
})).digest("hex");
verifyMutationIdentity(config, {
  ...unicodeRequest, bodyMarkdown: "Правка 🦊", operationId: unicodeId,
  activityId: unicodeRequest.activityId.replace(unicodeRequest.operationId, unicodeId),
} as never, "content_update");
assert.throws(
  () => verifyMutationIdentity(config, {
    ...unicodeRequest, bodyMarkdown: "Правка 🦊",
  } as never, "content_update"),
  /mutation operation identity mismatch/,
);
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
await assert.rejects(
  () => updateContent(noNetwork as never, config, {...updateRequest, renderOnly: false}),
  /NodeBB Update requires durable render-only delivery/,
);

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
