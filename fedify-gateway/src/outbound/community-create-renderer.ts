/** Pure, deterministic render of verified community Create intents.
 *
 * No Fedify network client, signing key, actor fetch or clock is available here.
 * Caller-provided stable identity must be validated before it can be persisted.
 */

import type { GatewayConfig } from "../config.js";
import type { RenderCommunityCreateRequest, RenderCommunityCreateResult } from "../types.js";

/** Build immutable community Create JSON without network requests or signing keys.
 *
 * Python owns both the verified remote software classification and the original
 * Group Follow. The render boundary validates caller-supplied IDs only; the
 * existing User Actor delivery worker later signs and submits this saved JSON.
 */
export function renderCommunityCreate(
  config: GatewayConfig,
  request: RenderCommunityCreateRequest,
): RenderCommunityCreateResult {
  const origin = new URL(config.fedifyOrigin).origin;
  const username = encodeURIComponent(request.actorUsername);
  const actor = `${origin}/actors/users/${username}`;
  const followers = `${actor}/followers`;
  const segment = request.kind === "comment" ? "comments" : "posts";
  const expectedObject = `${actor}/objects/${segment}/${request.operationId}`;
  const expectedCreate = `${actor}/activities/create/${segment}/${request.operationId}`;
  if (!/^[a-f0-9]{64}$/.test(request.operationId) ||
      request.objectId !== expectedObject || request.activityId !== expectedCreate) {
    throw new Error("community Create IDs must match caller operation digest and canonical User Actor");
  }
  if (!["nodebb", "threadiverse", "bridge"].includes(request.softwareProfile)) {
    throw new Error("remote software profile must be verified before rendering");
  }
  const published = new Date(request.publishedAt);
  if (!Number.isFinite(published.getTime()) || published.toISOString() !== request.publishedAt) {
    throw new Error("publishedAt must be a canonical millisecond UTC timestamp");
  }
  const group = new URL(request.communityActorUrl);
  if (group.protocol !== "https:" || group.hash || group.username || group.password) {
    throw new Error("community Create requires an HTTPS canonical Group actor URL");
  }
  if (request.kind === "comment" && (!request.inReplyToObjectId || !request.conversationRootObjectId)) {
    throw new Error("community comment requires an exact parent and conversation root");
  }
  if (request.kind !== "comment" && request.kind !== "post") {
    throw new Error("unsupported community Create kind");
  }
  const publicId = "https://www.w3.org/ns/activitystreams#Public";
  const kind = request.kind === "comment" || request.softwareProfile === "nodebb" ? "Note" : "Page";
  const object: Record<string, unknown> = {
    type: kind,
    id: request.objectId,
    url: request.objectId,
    attributedTo: actor,
    audience: group.href,
    to: [publicId, group.href],
    cc: [actor],
    source: { content: request.bodyMarkdown, mediaType: "text/markdown" },
    content: markdownToHtml(request.bodyMarkdown),
    published: request.publishedAt,
  };
  if (request.kind === "post") {
    object.name = request.title ?? "Untitled Discord Post";
  } else {
    object.inReplyTo = new URL(request.inReplyToObjectId!).href;
  }
  // NodeBB FEP-7888 threads are keyed by their canonical root object. Do not
  // manufacture remote /topic IDs or apply NodeBB context to Lemmy Pages.
  if (request.softwareProfile === "nodebb") {
    object.context = request.kind === "post" ? request.objectId : new URL(request.conversationRootObjectId!).href;
  }
  return {
    activityId: request.activityId,
    objectId: request.objectId,
    publishedAt: request.publishedAt,
    activityJson: {
      "@context": "https://www.w3.org/ns/activitystreams",
      type: "Create", id: request.activityId, actor,
      to: [publicId, group.href], cc: [actor, followers], object,
    },
  };
}


/** Convert markdown paragraphs to the existing unescaped community HTML policy. */
export function markdownToHtml(markdown: string): string {
  return markdown
    .split(/\n\n+/)
    .map((p) => `<p>${p.trim().replace(/\n/g, "<br>")}</p>`)
    .join("\n");
}
