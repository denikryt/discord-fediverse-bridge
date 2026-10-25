/**
 * Internal publishing and mutation route registration.
 *
 * Python owns content policy and durable state; these handlers validate the
 * existing wire payloads and delegate to gateway outbound use cases unchanged.
 */
import type { Hono } from "hono";

import type { GatewayConfig } from "../config.js";
import type { createGatewayFederation } from "../federation.js";
import {
  deleteContent,
  renderPersonalFeedCreate,
  updateContent,
} from "../federation-outbound.js";
import type {
  DeleteContentRequest,
  RenderPersonalFeedRequest,
  RenderCommunityRequest,
  UpdateContentRequest,
} from "../types.js";
import { hasValidInternalAuthorization } from "./internal-auth.js";
import { renderCommunityCreate } from "../outbound/community-renderer.js";

export interface InternalPublishRouteDependencies {
  config: GatewayConfig;
  fedify: ReturnType<typeof createGatewayFederation>;
}

/** Mount internal publish and Update/Delete command routes. */
export function registerInternalPublishRoutes(
  app: Hono,
  dependencies: InternalPublishRouteDependencies,
): void {
  const { config, fedify } = dependencies;

  app.post("/render-personal-feed", async (context) => {
    if (!hasValidInternalAuthorization(config, context.req.header("Authorization") ?? null)) {
      return context.json({ error: "invalid authorization" }, { status: 401 });
    }
    const payload = (await context.req.json()) as Partial<RenderPersonalFeedRequest>;
    if (
      typeof payload.actorUsername !== "string" ||
      typeof payload.operationId !== "string" ||
      typeof payload.activityId !== "string" ||
      typeof payload.objectId !== "string" ||
      typeof payload.bodyMarkdown !== "string" ||
      typeof payload.publishedAt !== "string" ||
      (payload.kind !== undefined && payload.kind !== "post" && payload.kind !== "comment")
    ) {
      return context.json({ error: "invalid personal-feed render request" }, { status: 400 });
    }
    try {
      return context.json(renderPersonalFeedCreate(config, payload as RenderPersonalFeedRequest));
    } catch (error) {
      return context.json({ error: error instanceof Error ? error.message : String(error) }, { status: 400 });
    }
  });

  app.post("/render-community", async (context) => {
    // Source identity and full audience are decided by Python, not a network
    // lookup. This endpoint cannot access the Fedify delivery context.
    if (!hasValidInternalAuthorization(config, context.req.header("Authorization") ?? null)) {
      return context.json({ error: "invalid authorization" }, { status: 401 });
    }
    const request = (await context.req.json()) as Partial<RenderCommunityRequest>;
    if (typeof request.actorUsername !== "string"
        || typeof request.communityActorUrl !== "string"
        || (request.kind !== "post" && request.kind !== "comment")
        || typeof request.bodyMarkdown !== "string"
        || typeof request.operationId !== "string"
        || typeof request.activityId !== "string"
        || typeof request.objectId !== "string"
        || typeof request.publishedAt !== "string") {
      return context.json({ error: "invalid render-only community Create request" }, { status: 400 });
    }
    try {
      return context.json(renderCommunityCreate(config, request as RenderCommunityRequest));
    } catch (error) {
      return context.json({ error: error instanceof Error ? error.message : String(error) }, { status: 400 });
    }
  });

  // Stage 04 removes the send-before-persist publication escape hatch.
  // Canonical authored Create must be rendered and committed in Python first,
  // then dispatched as frozen JSON via /send-user-actor-deliveries or the
  // Group-signed relay route. These obsolete routes cannot emit network I/O.
  for (const path of ["/publish", "/publish-local-community"]) {
    app.post(path, (context) => {
      if (!hasValidInternalAuthorization(config, context.req.header("Authorization") ?? null)) {
        return context.json({ error: "invalid authorization" }, { status: 401 });
      }
      return context.json({ error: "community publication requires durable render-first admission" }, { status: 410 });
    });
  }

  app.post("/update", async (context) => {
    // Python owns the edit policy; this endpoint owns the signed Update delivery.
    if (!hasValidInternalAuthorization(config, context.req.header("Authorization") ?? null)) {
      return context.json({ error: "invalid authorization" }, { status: 401 });
    }

    const payload = (await context.req.json()) as Partial<UpdateContentRequest>;
    if (
      typeof payload.actorUsername !== "string" ||
      (payload.publicationContext !== undefined &&
        payload.publicationContext !== "community" &&
        payload.publicationContext !== "personal_feed") ||
      (payload.publicationContext !== "personal_feed" && typeof payload.communityActorUrl !== "string") ||
      (payload.publicationContext === "personal_feed" && payload.communityActorUrl != null) ||
      typeof payload.apObjectId !== "string" ||
      (payload.kind !== "post" && payload.kind !== "comment") ||
      typeof payload.bodyMarkdown !== "string"
    ) {
      return context.json(
        { error: "actorUsername, communityActorUrl, apObjectId, kind, and bodyMarkdown are required" },
        { status: 400 },
      );
    }

    try {
      const result = await updateContent(fedify, config, {
        actorUsername: payload.actorUsername,
        publicationContext: payload.publicationContext ?? "community",
        communityActorUrl: typeof payload.communityActorUrl === "string" ? payload.communityActorUrl : null,
        conversationRootObjectId: typeof payload.conversationRootObjectId === "string" ? payload.conversationRootObjectId : null,
        apObjectId: payload.apObjectId,
        kind: payload.kind,
        bodyMarkdown: payload.bodyMarkdown,
        title: typeof payload.title === "string" && payload.title.length > 0 ? payload.title : null,
        inReplyToObjectId:
          typeof payload.inReplyToObjectId === "string" && payload.inReplyToObjectId.length > 0
            ? payload.inReplyToObjectId
            : null,
        operationId: typeof payload.operationId === "string" ? payload.operationId : null,
        activityId: typeof payload.activityId === "string" ? payload.activityId : null,
        mutationTimestamp: typeof payload.mutationTimestamp === "string" ? payload.mutationTimestamp : null,
        expectedRevision: typeof payload.expectedRevision === "number" ? payload.expectedRevision : null,
        nextRevision: typeof payload.nextRevision === "number" ? payload.nextRevision : null,
      });
      return context.json(result);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return context.json({ error: message }, { status: 500 });
    }
  });

  app.post("/delete", async (context) => {
    // Python owns the delete policy; this endpoint owns the signed Delete delivery.
    if (!hasValidInternalAuthorization(config, context.req.header("Authorization") ?? null)) {
      return context.json({ error: "invalid authorization" }, { status: 401 });
    }

    const payload = (await context.req.json()) as Partial<DeleteContentRequest>;
    if (
      typeof payload.actorUsername !== "string" ||
      (payload.publicationContext !== undefined &&
        payload.publicationContext !== "community" &&
        payload.publicationContext !== "personal_feed") ||
      (payload.publicationContext !== "personal_feed" && typeof payload.communityActorUrl !== "string") ||
      (payload.publicationContext === "personal_feed" && payload.communityActorUrl != null) ||
      typeof payload.apObjectId !== "string"
    ) {
      return context.json(
        { error: "actorUsername, communityActorUrl, and apObjectId are required" },
        { status: 400 },
      );
    }

    try {
      const result = await deleteContent(fedify, config, {
        actorUsername: payload.actorUsername,
        publicationContext: payload.publicationContext ?? "community",
        communityActorUrl: typeof payload.communityActorUrl === "string" ? payload.communityActorUrl : null,
        conversationRootObjectId: typeof payload.conversationRootObjectId === "string" ? payload.conversationRootObjectId : null,
        apObjectId: payload.apObjectId,
        operationId: typeof payload.operationId === "string" ? payload.operationId : null,
        activityId: typeof payload.activityId === "string" ? payload.activityId : null,
        mutationTimestamp: typeof payload.mutationTimestamp === "string" ? payload.mutationTimestamp : null,
        expectedRevision: typeof payload.expectedRevision === "number" ? payload.expectedRevision : null,
        nextRevision: typeof payload.nextRevision === "number" ? payload.nextRevision : null,
      });
      return context.json(result);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return context.json({ error: message }, { status: 500 });
    }
  });
}
