/**
 * Render-only canonical Update/Delete construction.
 *
 * This module must never send an activity: the caller atomically stores the
 * canonical mutation, exact activity and its authorized recipient snapshot
 * before the signed User Actor/Group worker performs network delivery.
 */

import { createHash } from "node:crypto";

import { Temporal } from "@js-temporal/polyfill";
import { Delete, Note, Page, Source, Update } from "@fedify/vocab";
import type { Federation } from "@fedify/fedify";

import type { GatewayConfig } from "../config.js";
import type { CanonicalMutationResult, DeleteContentRequest, UpdateContentRequest } from "../types.js";
import { buildGatewayUrl, buildUserActorUri } from "./gateway-url.js";
import { renderPublicActivityJson } from "./activity-json.js";
import { markdownToHtml } from "./content-publish.js";

function stableJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${stableJson(record[key])}`).join(",")}}`;
}

/** Verify deterministic mutation operation and activity identity fields. */
export function verifyMutationIdentity(config: GatewayConfig, request: UpdateContentRequest | DeleteContentRequest, kind: "content_update" | "content_delete"): void {
  if (request.operationId == null || request.activityId == null || request.expectedRevision == null || request.nextRevision == null) {
    throw new Error("mutation idempotency fields must be supplied together");
  }
  const requestShape: Record<string, unknown> = {
    actorUsername: request.actorUsername,
    publicationContext: request.publicationContext ?? "community",
    communityActorUrl: request.communityActorUrl,
    conversationRootObjectId: request.conversationRootObjectId ?? null,
    apObjectId: request.apObjectId,
    kind: "kind" in request ? request.kind : null,
    title: "title" in request ? request.title ?? null : null,
    bodyMarkdown: "bodyMarkdown" in request ? request.bodyMarkdown : null,
    inReplyToObjectId: "inReplyToObjectId" in request ? request.inReplyToObjectId ?? null : null,
    mutationTimestamp: request.mutationTimestamp ?? null,
  };
  const identity = stableJson({
    objectId: request.apObjectId,
    kind,
    expectedRevision: request.expectedRevision,
    nextRevision: request.nextRevision,
    request: requestShape,
  });
  const expectedOperationId = createHash("sha256").update(identity).digest("hex");
  if (expectedOperationId !== request.operationId) throw new Error("mutation operation identity mismatch");
  const objectKindSegment = "kind" in request && request.kind === "comment" ? "comments" : request.apObjectId.includes("/comments/") ? "comments" : "posts";
  const actionSegment = kind === "content_delete" ? "delete" : "update";
  const expectedActivityId = buildGatewayUrl(
    config,
    `/actors/users/${request.actorUsername}/activities/${actionSegment}/${objectKindSegment}/${expectedOperationId}`,
  ).href;
  if (expectedActivityId !== new URL(request.activityId).href) throw new Error("mutation activity identity mismatch");
}

function buildUpdateActivity(
  config: GatewayConfig,
  request: UpdateContentRequest,
): { activity: Update; activityId: URL } {
  /** Build one user-authored Update with the full object shape Lemmy expects. */
  verifyMutationIdentity(config, request, "content_update");
  const actorUri = buildUserActorUri(config, request.actorUsername);
  const objectId = new URL(request.apObjectId);
  // Identity was fully validated above. Never mint a new AP ID on retry.
  const activityId = new URL(request.activityId!);
  const htmlContent = markdownToHtml(request.bodyMarkdown);
  const source = new Source({
    content: request.bodyMarkdown,
    mediaType: "text/markdown",
  });
  const PUBLIC = new URL("https://www.w3.org/ns/activitystreams#Public");
  const personalFeed = request.publicationContext === "personal_feed";
  const community = request.communityActorUrl == null ? null : new URL(request.communityActorUrl);
  if (!personalFeed && community == null) {
    throw new Error("community mutation requires communityActorUrl");
  }
  if (personalFeed && community != null) {
    throw new Error("personal-feed mutation cannot carry communityActorUrl");
  }
  const followers = buildGatewayUrl(config, `/actors/users/${request.actorUsername}/followers`);
  if (request.mutationTimestamp == null) throw new Error("verified Update timestamp required");
  const updated = Temporal.Instant.from(request.mutationTimestamp);

  let object;
  if (request.kind === "post") {
    object = new Page({
      id: objectId,
      name: request.title ?? "Untitled Discord Post",
      attribution: actorUri,
      ...(community == null ? {} : { audience: community }),
      tos: community == null ? [PUBLIC] : [PUBLIC, community],
      ccs: personalFeed ? [followers] : [actorUri],
      source,
      content: htmlContent,
      updated,
      url: objectId,
    });
  } else {
    const noteParams: ConstructorParameters<typeof Note>[0] = {
      id: objectId,
      attribution: actorUri,
      ...(community == null ? {} : { audience: community }),
      tos: community == null ? [PUBLIC] : [PUBLIC, community],
      ccs: personalFeed ? [followers] : [actorUri],
      source,
      content: htmlContent,
      updated,
    };
    if (request.inReplyToObjectId) {
      noteParams.replyTarget = new URL(request.inReplyToObjectId);
    }
    object = new Note(noteParams);
  }

  return {
    activityId,
    activity: new Update({
      id: activityId,
      actor: actorUri,
      object,
      tos: community == null ? [PUBLIC] : [PUBLIC, community],
      ccs: [actorUri, followers],
    }),
  };
}

function buildDeleteActivity(
  config: GatewayConfig,
  request: DeleteContentRequest,
): { activity: Delete; activityId: URL } {
  /** Build one user-authored Delete using the Lemmy URL-object convention. */
  verifyMutationIdentity(config, request, "content_delete");
  const actorUri = buildUserActorUri(config, request.actorUsername);
  // Delete activity remains stable across restart and receipt loss.
  const activityId = new URL(request.activityId!);
  const PUBLIC = new URL("https://www.w3.org/ns/activitystreams#Public");
  const personalFeed = request.publicationContext === "personal_feed";
  const community = request.communityActorUrl == null ? null : new URL(request.communityActorUrl);
  if (!personalFeed && community == null) {
    throw new Error("community mutation requires communityActorUrl");
  }
  if (personalFeed && community != null) {
    throw new Error("personal-feed mutation cannot carry communityActorUrl");
  }
  const followers = buildGatewayUrl(config, `/actors/users/${request.actorUsername}/followers`);
  return {
    activityId,
    activity: new Delete({
      id: activityId,
      actor: actorUri,
      object: new URL(request.apObjectId),
      tos: community == null ? [PUBLIC] : [PUBLIC, community],
      ccs: [actorUri, followers],
    }),
  };
}

/** Render one immutable Update, with no recipient lookup or signed network send. */
export async function updateContent(
  _federation: Federation<GatewayConfig>,
  config: GatewayConfig,
  request: UpdateContentRequest,
): Promise<CanonicalMutationResult> {
  const { activity, activityId } = buildUpdateActivity(config, request);
  return {
    activityId: activityId.href,
    objectId: request.apObjectId,
    activityJson: await renderPublicActivityJson(activity),
  };
}

/** Render one immutable Delete; dispatch is owned by a post-commit worker. */
export async function deleteContent(
  _federation: Federation<GatewayConfig>,
  config: GatewayConfig,
  request: DeleteContentRequest,
): Promise<CanonicalMutationResult> {
  const { activity, activityId } = buildDeleteActivity(config, request);
  return {
    activityId: activityId.href,
    objectId: request.apObjectId,
    activityJson: await renderPublicActivityJson(activity),
  };
}
