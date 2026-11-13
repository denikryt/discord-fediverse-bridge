/**
 * Internal follow lifecycle route registration.
 *
 * These routes remain Python-only command surfaces guarded by the shared bearer
 * token. They preserve existing follow/unfollow request validation and errors.
 */
import type { Hono } from "hono";

import type { GatewayConfig } from "../config.js";
import type { createGatewayFederation } from "../federation.js";
import {
  acceptLocalCommunityFollow,
  acceptUserActorFollow,
  followCommunity,
  prepareCommunityFollow,
  sendPreparedCommunityFollow,
  unfollowCommunity,
} from "../federation-outbound.js";
import type { AcceptLocalCommunityFollowRequest, AcceptUserActorFollowRequest } from "../types.js";
import { hasValidInternalAuthorization } from "./internal-auth.js";

export interface InternalFollowRouteDependencies {
  config: GatewayConfig;
  fedify: ReturnType<typeof createGatewayFederation>;
}

/** Mount internal follow routes in their previous server order. */
export function registerInternalFollowRoutes(
  app: Hono,
  dependencies: InternalFollowRouteDependencies,
): void {
  const { config, fedify } = dependencies;

  app.post("/prepare-community-follow", async (context) => {
    // Discovery is allowed here, but this endpoint must never send an AP Follow.
    if (!hasValidInternalAuthorization(config, context.req.header("Authorization") ?? null)) {
      return context.json({ error: "invalid authorization" }, { status: 401 });
    }
    const { communityActorUrl } = await context.req.json();
    if (typeof communityActorUrl !== "string" || !communityActorUrl) {
      return context.json({ error: "communityActorUrl is required" }, { status: 400 });
    }
    try {
      return context.json(await prepareCommunityFollow(config, communityActorUrl));
    } catch (error) {
      return context.json({ error: error instanceof Error ? error.message : "actor lookup failed" }, { status: 500 });
    }
  });

  app.post("/send-prepared-community-follow", async (context) => {
    // The caller already committed the exact actor/inbox/Follow tuple.  This
    // route cannot generate a new ID or pick a different remote recipient.
    if (!hasValidInternalAuthorization(config, context.req.header("Authorization") ?? null)) {
      return context.json({ error: "invalid authorization" }, { status: 401 });
    }
    const payload = await context.req.json() as Record<string, unknown>;
    if (["communityActorUrl", "communityInboxUrl", "followActivityId"].some(
      (field) => typeof payload[field] !== "string" || !payload[field],
    )) {
      return context.json({ error: "prepared Follow tuple is incomplete" }, { status: 400 });
    }
    try {
      await sendPreparedCommunityFollow(fedify, config, {
        communityActorUrl: payload.communityActorUrl as string,
        communityInboxUrl: payload.communityInboxUrl as string,
        followActivityId: payload.followActivityId as string,
      });
      return context.json({ ok: true });
    } catch (error) {
      // Report the failure at the gateway boundary. A 500 does not prove that
      // a potentially started network delivery was rejected by the remote Group.
      const reason = error instanceof Error ? error.message : "Follow send failed";
      console.error("[Follow] Prepared Follow dispatch failed", {
        communityActorUrl: payload.communityActorUrl,
        followActivityId: payload.followActivityId,
        error: reason,
      });
      return context.json({ error: reason }, { status: 500 });
    }
  });

  app.post("/follow-community", async (context) => {
    // This endpoint exists as an operational bootstrap path until follow logic
    // is driven from the bot itself.
    if (!hasValidInternalAuthorization(config, context.req.header("Authorization") ?? null)) {
      return context.json({ error: "invalid authorization" }, { status: 401 });
    }
    const { communityActorUrl } = await context.req.json();

    if (!communityActorUrl || typeof communityActorUrl !== "string") {
      return context.json(
        { error: "communityActorUrl is required and must be a string" },
        { status: 400 },
      );
    }

    try {
      const result = await followCommunity(fedify, config, communityActorUrl);
      return context.json(result);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return context.json({ error: message }, { status: 500 });
    }
  });

  app.post("/unfollow-community", async (context) => {
    if (!hasValidInternalAuthorization(config, context.req.header("Authorization") ?? null)) {
      return context.json({ error: "invalid authorization" }, { status: 401 });
    }
    const { communityActorUrl, followActivityId, communityInboxUrl } = await context.req.json();

    if (!communityActorUrl || typeof communityActorUrl !== "string") {
      return context.json(
        { error: "communityActorUrl is required and must be a string" },
        { status: 400 },
      );
    }
    if (!followActivityId || typeof followActivityId !== "string") {
      return context.json(
        { error: "followActivityId is required and must be a string" },
        { status: 400 },
      );
    }

    if (communityInboxUrl !== undefined && (typeof communityInboxUrl !== "string" || !communityInboxUrl)) {
      return context.json({ error: "communityInboxUrl must be a nonempty URL" }, { status: 400 });
    }
    try {
      await unfollowCommunity(fedify, config, communityActorUrl, followActivityId, communityInboxUrl);
      return context.json({ ok: true });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return context.json({ error: message }, { status: 500 });
    }
  });

  app.post("/accept-user-actor-follow", async (context) => {
    if (!hasValidInternalAuthorization(config, context.req.header("Authorization") ?? null)) {
      return context.json({ error: "invalid authorization" }, { status: 401 });
    }
    const payload = (await context.req.json()) as Partial<AcceptUserActorFollowRequest>;
    if (
      typeof payload.actorUsername !== "string" ||
      typeof payload.actorUrl !== "string" ||
      typeof payload.remoteActorId !== "string" ||
      typeof payload.remoteInboxUrl !== "string" ||
      typeof payload.followActivityId !== "string"
    ) {
      return context.json({ error: "invalid user actor follow accept request" }, { status: 400 });
    }
    try {
      await acceptUserActorFollow(fedify, config, payload as AcceptUserActorFollowRequest);
      return context.json({ status: "ok" });
    } catch (error) {
      return context.json({ error: error instanceof Error ? error.message : String(error) }, { status: 500 });
    }
  });

  app.post("/accept-local-community-follow", async (context) => {
    if (!hasValidInternalAuthorization(config, context.req.header("Authorization") ?? null)) {
      return context.json({ error: "invalid authorization" }, { status: 401 });
    }

    const payload = (await context.req.json()) as Partial<AcceptLocalCommunityFollowRequest>;
    if (
      typeof payload.communitySlug !== "string" ||
      typeof payload.communityActorUrl !== "string" ||
      typeof payload.remoteActorId !== "string" ||
      typeof payload.remoteInboxUrl !== "string" ||
      typeof payload.followActivityId !== "string"
    ) {
      return context.json(
        {
          error:
            "communitySlug, communityActorUrl, remoteActorId, remoteInboxUrl, and followActivityId are required",
        },
        { status: 400 },
      );
    }

    try {
      await acceptLocalCommunityFollow(fedify, config, {
        communitySlug: payload.communitySlug,
        communityActorUrl: payload.communityActorUrl,
        remoteActorId: payload.remoteActorId,
        remoteInboxUrl: payload.remoteInboxUrl,
        followActivityId: payload.followActivityId,
      });
      return context.json({ status: "ok" });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return context.json({ error: message }, { status: 500 });
    }
  });
}
