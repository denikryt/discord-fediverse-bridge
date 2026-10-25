/** Pure canonical ActivityPub Create renderer for both community modes.
 *
 * It may never discover recipients, load signing keys or use sendActivity.
 * The persisted Python source controls the exact stable operation/ActivityPub
 * identity and signed User/Group transport begins only after SQLite commit.
 */
import type { GatewayConfig } from "../config.js";
import type { RenderCommunityRequest, RenderCommunityResult } from "../types.js";

const PUBLIC = "https://www.w3.org/ns/activitystreams#Public";

/** Preserve the currently accepted community markdown HTML representation. */
function markdownToHtml(markdown: string): string {
  return markdown.split(/\n\n+/).map((p) => `<p>${p.trim().replace(/\n/g, "<br>")}</p>`).join("\n");
}

/** Render exactly the originally verified source-created object, without I/O. */
export function renderCommunityCreate(
  config: Pick<GatewayConfig, "fedifyOrigin">,
  request: RenderCommunityRequest,
): RenderCommunityResult {
  const base = new URL(config.fedifyOrigin).origin;
  const username = encodeURIComponent(request.actorUsername);
  const actorId = new URL(`/actors/users/${username}`, base).href;
  const collection = request.kind === "comment" ? "comments" : "posts";
  const expectedObject = new URL(`/actors/users/${username}/objects/${collection}/${request.operationId}`, base).href;
  const expectedActivity = new URL(`/actors/users/${username}/activities/create/${collection}/${request.operationId}`, base).href;
  if (!/^[a-f0-9]{64}$/.test(request.operationId)
      || request.objectId !== expectedObject || request.activityId !== expectedActivity) {
    throw new Error("community operation identity does not match immutable source ID");
  }
  if (request.kind !== "post" && request.kind !== "comment") {
    throw new Error("unsupported community content kind");
  }
  if (typeof request.actorUsername !== "string" || request.actorUsername.length === 0
      || typeof request.bodyMarkdown !== "string" || request.bodyMarkdown.length === 0) {
    throw new Error("missing canonical community author or body");
  }
  if (request.kind === "comment" && (typeof request.inReplyToObjectId !== "string" || !request.inReplyToObjectId)) {
    throw new Error("comment requires a verified canonical parent object");
  }
  const published = new Date(request.publishedAt);
  if (Number.isNaN(published.getTime()) || published.toISOString() !== request.publishedAt) {
    throw new Error("publishedAt must be UTC RFC3339 with milliseconds");
  }
  const community = new URL(request.communityActorUrl).href;
  if (!community.startsWith("https://")) throw new Error("community actor requires HTTPS");
  const followers = `${actorId}/followers`;
  const object: Record<string, unknown> = {
    id: request.objectId,
    type: request.kind === "post" ? "Page" : "Note",
    attributedTo: actorId,
    audience: community,
    to: [PUBLIC, community],
    cc: [actorId],
    source: { mediaType: "text/markdown", content: request.bodyMarkdown },
    content: markdownToHtml(request.bodyMarkdown),
    published: request.publishedAt,
    url: request.objectId,
  };
  if (request.kind === "post") object.name = request.title || "Untitled Discord Post";
  else object.inReplyTo = new URL(request.inReplyToObjectId!).href;
  return {
    activityId: request.activityId,
    objectId: request.objectId,
    communityActorUrl: community,
    publishedAt: request.publishedAt,
    activityJson: {
      "@context": "https://www.w3.org/ns/activitystreams",
      id: request.activityId,
      type: "Create",
      actor: actorId,
      to: [PUBLIC, community],
      cc: [actorId, followers],
      object,
    },
  };
}
