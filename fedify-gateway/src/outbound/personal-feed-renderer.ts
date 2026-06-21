/**
 * Pure canonical renderer for personal-feed Create activities.
 *
 * Python owns persistence and later delivery. This module validates canonical
 * ids and timestamps, escapes Discord-authored text for public HTML, and never
 * imports signing keys, network delivery, or subscriber state.
 */

import type { GatewayConfig } from "../config.js";
import type { RenderPersonalFeedRequest, RenderPersonalFeedResult } from "../types.js";

const ACTIVITYSTREAMS_PUBLIC_IRI = "https://www.w3.org/ns/activitystreams#Public";

function escapeHtml(value: string): string {
  // User-authored Discord text crosses a public HTML boundary; escape before
  // adding paragraph and line-break markup.
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/** Render and validate one canonical personal-feed Create activity. */
export function renderPersonalFeedCreate(
  config: GatewayConfig,
  request: RenderPersonalFeedRequest,
): RenderPersonalFeedResult {
  // Stage 8 keeps one render-only boundary for both personal-feed roots and
  // comments. Python owns persistence and delivery; the gateway owns canonical
  // ActivityStreams construction without loading signing keys.
  const origin = new URL(config.fedifyOrigin).origin;
  const actorId = new URL(`/actors/users/${encodeURIComponent(request.actorUsername)}`, origin).href;
  const followersId = `${actorId}/followers`;
  const kind = request.kind ?? "post";
  const objectSegment = kind === "comment" ? "comments" : "posts";
  const expectedObjectId = new URL(`/actors/users/${encodeURIComponent(request.actorUsername)}/objects/${objectSegment}/${request.operationId}`, origin).href;
  const expectedActivityId = new URL(`/actors/users/${encodeURIComponent(request.actorUsername)}/activities/create/${objectSegment}/${request.operationId}`, origin).href;
  if (request.objectId !== expectedObjectId || request.activityId !== expectedActivityId) {
    throw new Error("personal-feed IDs do not match canonical origin/path");
  }
  if (!/^[a-f0-9]{64}$/.test(request.operationId)) {
    throw new Error("operationId must be a lowercase SHA-256 hex digest");
  }
  if (kind === "comment" && (typeof request.inReplyToObjectId !== "string" || request.inReplyToObjectId.length === 0)) {
    throw new Error("personal-feed comments require inReplyToObjectId");
  }
  const parsedPublished = new Date(request.publishedAt);
  if (Number.isNaN(parsedPublished.getTime()) || parsedPublished.toISOString() !== request.publishedAt) {
    throw new Error("publishedAt must be canonical UTC RFC3339 with milliseconds");
  }
  const object: Record<string, unknown> = {
    "@context": "https://www.w3.org/ns/activitystreams",
    type: "Note",
    id: request.objectId,
    url: request.objectId,
    attributedTo: actorId,
    to: [ACTIVITYSTREAMS_PUBLIC_IRI],
    cc: [followersId],
    source: { mediaType: "text/markdown", content: request.bodyMarkdown },
    content: request.bodyMarkdown
      .split(/\n\n+/)
      .map((paragraph) => `<p>${escapeHtml(paragraph.trim()).replace(/\n/g, "<br>")}</p>`)
      .join("\n"),
    published: request.publishedAt,
  };
  if (kind === "comment") object.inReplyTo = request.inReplyToObjectId;
  return {
    activityId: request.activityId,
    objectId: request.objectId,
    publishedAt: request.publishedAt,
    activityJson: {
      "@context": "https://www.w3.org/ns/activitystreams",
      type: "Create",
      id: request.activityId,
      actor: actorId,
      to: [ACTIVITYSTREAMS_PUBLIC_IRI],
      cc: [actorId, followersId],
      object,
    },
  };
}
