/**
 * Canonical Update/Delete construction and delivery mode dispatch.
 *
 * This module owns deterministic mutation identity verification and the exact
 * ActivityPub Update/Delete shapes. Local-community recipient fanout is
 * delegated to the fanout module and remote-community fallback remains Fedify
 * sendActivity, preserving existing behavior.
 */

import { createHash } from "node:crypto";

import { Temporal } from "@js-temporal/polyfill";
import { Delete, Note, Page, Source, Update } from "@fedify/vocab";
import type { Federation } from "@fedify/fedify";

import type { GatewayConfig } from "../config.js";
import type { CanonicalMutationResult, DeleteContentRequest, UpdateContentRequest } from "../types.js";
import { buildGatewayUrl, buildUserActorUri } from "./gateway-url.js";
import { renderPublicActivityJson } from "./activity-json.js";
import { fetchRemoteCommunity } from "./remote-actor.js";
import { markdownToHtml } from "./content-publish.js";
import { maybeDeliverLocalCommunityMutation } from "./local-community-fanout.js";

function stableJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${stableJson(record[key])}`).join(",")}}`;
}

/** Verify deterministic mutation operation and activity identity fields. */
export function verifyMutationIdentity(config: GatewayConfig, request: UpdateContentRequest | DeleteContentRequest, kind: "content_update" | "content_delete"): void {
  if (request.operationId == null && request.activityId == null) return;
  if (request.operationId == null || request.activityId == null || request.expectedRevision == null || request.nextRevision == null) {
    throw new Error("mutation idempotency fields must be supplied together");
  }
  const requestShape: Record<string, unknown> = {
    actorUsername: request.actorUsername,
    communityActorUrl: request.communityActorUrl,
    apObjectId: request.apObjectId,
    kind: "kind" in request ? request.kind : null,
    title: "title" in request ? request.title ?? null : null,
    bodyMarkdown: "bodyMarkdown" in request ? request.bodyMarkdown : null,
    inReplyToObjectId: "inReplyToObjectId" in request ? request.inReplyToObjectId ?? null : null,
    targetInboxUrls: request.targetInboxUrls ?? null,
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
  const activityId = request.activityId == null
    ? buildGatewayUrl(
        config,
        `/actors/users/${request.actorUsername}/activities/update/${request.kind === "comment" ? "comments" : "posts"}/${Date.now()}-${Math.random().toString(36).slice(2)}`,
      )
    : new URL(request.activityId);
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
  const updated = request.mutationTimestamp == null
    ? Temporal.Now.instant()
    : Temporal.Instant.from(request.mutationTimestamp);

  let object;
  if (request.kind === "post" && request.softwareProfile !== "nodebb") {
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
      ...(request.kind === "post" ? { name: request.title ?? "Untitled Discord Post" } : {}),
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
  const deleteKindSegment = request.apObjectId.includes("/comments/") ? "comments" : "posts";
  const activityId = request.activityId == null
    ? buildGatewayUrl(
        config,
        `/actors/users/${request.actorUsername}/activities/delete/${deleteKindSegment}/${Date.now()}-${Math.random().toString(36).slice(2)}`,
      )
    : new URL(request.activityId);
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

/** Refuse any unproven render-only mutation at the gateway boundary. */
function validateDirectMutationRender(request: UpdateContentRequest | DeleteContentRequest): void {
  if (request.publicationContext !== "community" ||
      !request.communityActorUrl ||
      !request.operationId || !request.activityId ||
      request.targetInboxUrls != null ||
      !["nodebb", "threadiverse", "bridge"].includes(request.softwareProfile ?? "")) {
    throw new Error("remote Group mutation render requires immutable verified Create provenance");
  }
  const group = new URL(request.communityActorUrl);
  if (group.protocol !== "https:" || group.hash || group.username || group.password) {
    throw new Error("remote Group mutation requires an HTTPS canonical actor");
  }
  if (request.softwareProfile === "nodebb" &&
      (!request.conversationRootObjectId ||
       new URL(request.conversationRootObjectId).protocol !== "https:")) {
    throw new Error("NodeBB mutation requires the persisted conversation root");
  }
}

/** Preserve the persisted NodeBB conversation root in the signed JSON payload.
 *
 * Fedify's Note constructor currently drops `context` during serialization.
 * The render-only community path signs the resulting JSON, not the transient
 * Note object, so add the verified root after serialization and before commit.
 */
async function serializeUpdate(
  activity: Update, request: UpdateContentRequest,
): Promise<Record<string, unknown>> {
  const json = await renderPublicActivityJson(activity);
  if (request.softwareProfile === "nodebb" && request.conversationRootObjectId) {
    const obj = json.object;
    if (obj == null || typeof obj !== "object" || Array.isArray(obj) ||
        (obj as Record<string, unknown>).type !== "Note" ||
        (obj as Record<string, unknown>).id !== request.apObjectId) {
      throw new Error("NodeBB Update is missing its canonical Note identity");
    }
    (obj as Record<string, unknown>).context = new URL(request.conversationRootObjectId).href;
  }
  return json;
}

/** Build and deliver or render one canonical content Update activity. */
export async function updateContent(
  federation: Federation<GatewayConfig>,
  config: GatewayConfig,
  request: UpdateContentRequest,
): Promise<CanonicalMutationResult> {
  // Update delivery mirrors publish delivery: same actor URI, same community
  // fetch, but wraps the object in an Update activity instead of Create.
  // NodeBB context is preserved in the saved signed JSON only; the old
  // Fedify.sendActivity object path cannot serialize it and must fail closed.
  if (request.softwareProfile === "nodebb" && request.publicationContext === "community" &&
      request.renderOnly !== true) {
    throw new Error("NodeBB Update requires durable render-only delivery");
  }
  const builtUpdate = buildUpdateActivity(config, request);
  if (request.renderOnly === true) {
    // Unlike the legacy remote path, this call must not perform discovery or
    // Fedify.sendActivity. Python commits the canonical mutation first, then
    // the existing User Actor worker authorizes its original Group recipient.
    validateDirectMutationRender(request);
    return {
      activityId: builtUpdate.activityId.href,
      objectId: request.apObjectId,
      activityJson: await serializeUpdate(builtUpdate.activity, request),
    };
  }
  if (request.publicationContext === "personal_feed") {
    return {
      activityId: builtUpdate.activityId.href,
      objectId: request.apObjectId,
      activityJson: await serializeUpdate(builtUpdate.activity, request),
    };
  }
  if (request.communityActorUrl == null) throw new Error("community mutation requires communityActorUrl");
  if (await maybeDeliverLocalCommunityMutation(config, request.communityActorUrl, builtUpdate.activity, request.targetInboxUrls)) {
    return {
      activityId: builtUpdate.activityId.href,
      objectId: request.apObjectId,
      activityJson: await serializeUpdate(builtUpdate.activity, request),
    };
  }

  const ctx = federation.createContext(new URL(config.fedifyOrigin), config);
  const { communityId, inboxUrl } = await fetchRemoteCommunity(request.communityActorUrl);

  console.log("[Update] Sending Update activity:", {
    actorUsername: request.actorUsername,
    kind: request.kind,
    apObjectId: request.apObjectId,
    inReplyToObjectId: request.inReplyToObjectId,
    bodyMarkdown: request.bodyMarkdown,
    communityId,
    activityId: builtUpdate.activityId.toString(),
  });

  try {
    const activityJson = await builtUpdate.activity.toJsonLd();
    console.log("[Update] Activity JSON-LD (before sendActivity):", JSON.stringify(activityJson, null, 2));

    await ctx.sendActivity(
      { username: request.actorUsername },
      { id: new URL(communityId), inboxId: new URL(inboxUrl) },
      builtUpdate.activity,
    );
    console.log("[Update] sendActivity completed successfully");
    return {
      activityId: builtUpdate.activityId.href,
      objectId: request.apObjectId,
      activityJson: await serializeUpdate(builtUpdate.activity, request),
    };
  } catch (err) {
    console.error("[Update] sendActivity failed:", err);
    throw err;
  }
}

/** Build and deliver or render one canonical content Delete activity. */
export async function deleteContent(
  federation: Federation<GatewayConfig>,
  config: GatewayConfig,
  request: DeleteContentRequest,
): Promise<CanonicalMutationResult> {
  // Delete delivery uses the AP object URL as the object field (string form, not
  // a full object), matching the Lemmy federation protocol for Delete activities.
  const builtDelete = buildDeleteActivity(config, request);
  if (request.renderOnly === true) {
    validateDirectMutationRender(request);
    return {
      activityId: builtDelete.activityId.href,
      objectId: request.apObjectId,
      activityJson: await renderPublicActivityJson(builtDelete.activity),
    };
  }
  if (request.publicationContext === "personal_feed") {
    return {
      activityId: builtDelete.activityId.href,
      objectId: request.apObjectId,
      activityJson: await renderPublicActivityJson(builtDelete.activity),
    };
  }
  if (request.communityActorUrl == null) throw new Error("community mutation requires communityActorUrl");
  if (await maybeDeliverLocalCommunityMutation(config, request.communityActorUrl, builtDelete.activity, request.targetInboxUrls)) {
    return {
      activityId: builtDelete.activityId.href,
      objectId: request.apObjectId,
      activityJson: await renderPublicActivityJson(builtDelete.activity),
    };
  }

  const ctx = federation.createContext(new URL(config.fedifyOrigin), config);
  const { communityId, inboxUrl } = await fetchRemoteCommunity(request.communityActorUrl);

  console.log("[Delete] Sending Delete activity:", {
    actorUsername: request.actorUsername,
    apObjectId: request.apObjectId,
    communityId,
    activityId: builtDelete.activityId.toString(),
  });

  try {
    await ctx.sendActivity(
      { username: request.actorUsername },
      { id: new URL(communityId), inboxId: new URL(inboxUrl) },
      builtDelete.activity,
    );
    console.log("[Delete] sendActivity completed successfully");
    return {
      activityId: builtDelete.activityId.href,
      objectId: request.apObjectId,
      activityJson: await renderPublicActivityJson(builtDelete.activity),
    };
  } catch (err) {
    console.error("[Delete] sendActivity failed:", err);
    throw err;
  }
}
