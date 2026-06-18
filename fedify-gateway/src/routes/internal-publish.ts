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
  publishContent,
  publishLocalCommunityContent,
  renderPersonalFeedCreate,
  updateContent,
} from "../federation-outbound.js";
import type {
  DeleteContentRequest,
  PublishContentRequest,
  PublishLocalCommunityContentRequest,
  RenderPersonalFeedRequest,
  UpdateContentRequest,
} from "../types.js";
import { hasValidInternalAuthorization } from "./internal-auth.js";

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

  app.post("/publish", async (context) => {
    // Python owns the Discord-side publish policy, while this endpoint owns the
    // signed user-actor Create delivery through Fedify.
    if (!hasValidInternalAuthorization(config, context.req.header("Authorization") ?? null)) {
      return context.json({ error: "invalid authorization" }, { status: 401 });
    }

    const payload = (await context.req.json()) as Partial<PublishContentRequest>;
    if (
      typeof payload.actorUsername !== "string" ||
      typeof payload.communityActorUrl !== "string" ||
      (payload.kind !== "post" && payload.kind !== "comment") ||
      typeof payload.bodyMarkdown !== "string"
    ) {
      return context.json(
        { error: "actorUsername, communityActorUrl, kind, and bodyMarkdown are required" },
        { status: 400 },
      );
    }
    if (payload.kind === "comment" && typeof payload.inReplyToObjectId !== "string") {
      return context.json({ error: "comment publish requires inReplyToObjectId" }, { status: 400 });
    }

    try {
      const result = await publishContent(fedify, config, {
        actorUsername: payload.actorUsername,
        communityActorUrl: payload.communityActorUrl,
        kind: payload.kind,
        title: typeof payload.title === "string" && payload.title.length > 0 ? payload.title : null,
        bodyMarkdown: payload.bodyMarkdown,
        inReplyToObjectId: typeof payload.inReplyToObjectId === "string" ? payload.inReplyToObjectId : null,
      });
      return context.json(result);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return context.json({ error: message }, { status: 500 });
    }
  });

  app.post("/publish-local-community", async (context) => {
    // Local-community publishes fan out one signed Create to every accepted
    // follower inbox of the Discord-backed community.
    if (!hasValidInternalAuthorization(config, context.req.header("Authorization") ?? null)) {
      return context.json({ error: "invalid authorization" }, { status: 401 });
    }

    const payload = (await context.req.json()) as Partial<PublishLocalCommunityContentRequest>;
    if (
      typeof payload.actorUsername !== "string" ||
      typeof payload.communityActorUrl !== "string" ||
      (payload.kind !== "post" && payload.kind !== "comment") ||
      typeof payload.bodyMarkdown !== "string"
    ) {
      return context.json(
        { error: "actorUsername, communityActorUrl, kind, and bodyMarkdown are required" },
        { status: 400 },
      );
    }
    if (payload.kind === "comment" && typeof payload.inReplyToObjectId !== "string") {
      return context.json({ error: "comment publish requires inReplyToObjectId" }, { status: 400 });
    }

    try {
      const result = await publishLocalCommunityContent(fedify, config, {
        actorUsername: payload.actorUsername,
        communityActorUrl: payload.communityActorUrl,
        kind: payload.kind,
        title: typeof payload.title === "string" && payload.title.length > 0 ? payload.title : null,
        bodyMarkdown: payload.bodyMarkdown,
        inReplyToObjectId: typeof payload.inReplyToObjectId === "string" ? payload.inReplyToObjectId : null,
        targetInboxUrls: Array.isArray(payload.targetInboxUrls)
          ? payload.targetInboxUrls.filter((value): value is string => typeof value === "string")
          : null,
      });
      return context.json(result);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return context.json({ error: message }, { status: 500 });
    }
  });

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
        targetInboxUrls: Array.isArray(payload.targetInboxUrls)
          ? payload.targetInboxUrls.filter((value): value is string => typeof value === "string")
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
        targetInboxUrls: Array.isArray(payload.targetInboxUrls)
          ? payload.targetInboxUrls.filter((value): value is string => typeof value === "string")
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
}
