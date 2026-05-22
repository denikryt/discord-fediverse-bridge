import { createHash } from "node:crypto";
import { pathToFileURL } from "node:url";

import { serve } from "@hono/node-server";
import { federation as fedifyMiddleware } from "@fedify/hono";
import { Hono } from "hono";

import { storeRawActivity } from "./activitypub-raw-cache.js";
import {
  buildEmptyOrderedCollection,
  buildLocalCommunityGroupActor,
  buildUserPersonActor,
} from "./actors.js";
import {
  getBridgeActorIdentity,
  hasLocalActor,
  loadLocalCommunityIdentity,
  loadUserActorIdentity,
} from "./actor-store.js";
import { buildLocalCommunityPublicKeyCarrier } from "./local-community-keys.js";
import {
  type GatewayConfig,
  type GatewayContextData,
  loadConfig,
} from "./config.js";
import {
  loadPublishedActivityObjectByActivityId,
  loadPublishedActivityObjectByObjectId,
} from "./db.js";
import { createGatewayFederation } from "./federation.js";
import {
  acceptLocalCommunityFollow,
  deleteContent,
  followCommunity,
  publishContent,
  publishLocalCommunityContent,
  sendLocalCommunityRelay,
  unfollowCommunity,
  updateContent,
} from "./federation-outbound.js";
import {
  buildPublishedActivityObjectJson,
  buildPublishedCreateActivityJson,
} from "./published-objects.js";
import type {
  AcceptLocalCommunityFollowRequest,
  DeleteContentRequest,
  PublishContentRequest,
  PublishLocalCommunityContentRequest,
  SendLocalCommunityRelayRequest,
  UpdateContentRequest,
} from "./types.js";
import { buildWebFingerDocument } from "./webfinger.js";
import { appendDebugFileLog, initializeDebugFileLog, installDebugFetchLogging } from "./debug-file-log.js";

// server.ts owns the operator-facing HTTP surface of the gateway: health,
// manual follow, inbox logging, and Fedify middleware wiring.
export function createGatewayApp(config: GatewayConfig): Hono {
  const fedify = createGatewayFederation(config);
  const app = new Hono();
  const isDebug = config.logLevel === "debug";
  const debugLogPath = initializeDebugFileLog(config);
  installDebugFetchLogging(config);
  if (debugLogPath != null) {
    console.log(`[DebugFileLog] Writing gateway debug diagnostics to ${debugLogPath}`);
  }

  if (isDebug) {
    // Debug access logging is intentionally gated because it can be noisy, but
    // it remains available to verify which ActivityPub URLs Mastodon fetches.
    app.use("*", async (context, next) => {
      const startedAt = Date.now();
      const method = context.req.method;
      const path = context.req.path;
      const accept = context.req.header("accept") ?? "";
      const userAgent = context.req.header("user-agent") ?? "";
      const forwardedFor =
        context.req.header("cf-connecting-ip") ??
        context.req.header("x-forwarded-for") ??
        "";

      await next();

      console.log("[HTTP]", {
        method,
        path,
        status: context.res.status,
        durationMs: Date.now() - startedAt,
        accept,
        userAgent,
        forwardedFor,
      });
    });
  }

  app.get("/healthz", (context) => {
    return context.json({ status: "ok" });
  });

  app.get("/.well-known/webfinger", async (context) => {
    const resource = context.req.query("resource");
    if (!resource) {
      return context.json({ error: "resource query parameter is required" }, 400);
    }

    const webFingerDocument = await buildWebFingerDocument(config, resource);
    if (webFingerDocument == null) {
      return context.json({ error: "resource not found" }, 404);
    }
    return context.newResponse(JSON.stringify(webFingerDocument), 200, {
      "Content-Type": "application/jrd+json",
    });
  });

  app.get("/users/:username", async (context) => {
    const username = context.req.param("username");
    const actor = await buildRegisteredUserActorDocument(config, fedify, username);
    if (actor == null) {
      return context.json({ error: "user actor not found" }, 404);
    }
    return activityJsonResponse(await actor.toJsonLd());
  });

  app.get("/communities/:slug", async (context) => {
    const slug = context.req.param("slug");
    const actor = await buildLocalCommunityActorDocument(config, fedify, slug);
    if (actor == null) {
      return context.json({ error: "community actor not found" }, 404);
    }
    return activityJsonResponse(await actor.toJsonLd());
  });

  app.get("/c/:slug", async (context) => {
    const slug = context.req.param("slug");
    const actor = await buildLocalCommunityActorDocument(config, fedify, slug);
    if (actor == null) {
      return context.json({ error: "community actor not found" }, 404);
    }
    return activityJsonResponse(await actor.toJsonLd());
  });

  app.get("/communities/:slug/outbox", async (context) => {
    const slug = context.req.param("slug");
    const community = await loadLocalCommunityIdentity(config, slug);
    if (community == null) {
      return context.json({ error: "community actor not found" }, 404);
    }
    return activityJsonResponse(
      await buildEmptyOrderedCollection(
        new URL(`/communities/${slug}/outbox`, config.fedifyOrigin),
      ).toJsonLd(),
    );
  });

  app.get("/communities/:slug/followers", async (context) => {
    const slug = context.req.param("slug");
    const community = await loadLocalCommunityIdentity(config, slug);
    if (community == null) {
      return context.json({ error: "community actor not found" }, 404);
    }
    return activityJsonResponse(
      await buildEmptyOrderedCollection(
        new URL(`/communities/${slug}/followers`, config.fedifyOrigin),
      ).toJsonLd(),
    );
  });

  app.get("/communities/:slug/post/:objectId", async (context) => {
    const object = await loadPublishedObjectForRequest(config, context.req.path);
    if (object == null || object.kind !== "post") {
      return context.json({ error: "published post not found" }, 404);
    }
    return activityJsonResponse(buildPublishedActivityObjectJson(object));
  });

  app.get("/communities/:slug/comment/:objectId", async (context) => {
    const object = await loadPublishedObjectForRequest(config, context.req.path);
    if (object == null || object.kind !== "comment") {
      return context.json({ error: "published comment not found" }, 404);
    }
    return activityJsonResponse(buildPublishedActivityObjectJson(object));
  });

  app.get("/users/:username/outbox", async (context) => {
    const username = context.req.param("username");
    if ((await loadUserActorIdentity(config, username)) == null) {
      return context.json({ error: "user actor not found" }, 404);
    }
    return activityJsonResponse(
      await buildEmptyOrderedCollection(
        new URL(`/users/${username}/outbox`, config.fedifyOrigin),
      ).toJsonLd(),
    );
  });

  app.get("/users/:username/followers", async (context) => {
    const username = context.req.param("username");
    if ((await loadUserActorIdentity(config, username)) == null) {
      return context.json({ error: "user actor not found" }, 404);
    }
    return activityJsonResponse(
      await buildEmptyOrderedCollection(
        new URL(`/users/${username}/followers`, config.fedifyOrigin),
      ).toJsonLd(),
    );
  });


  app.get("/users/:username/activities/create/post/:activityId", async (context) => {
    const activity = await loadPublishedCreateActivityForRequest(
      config,
      context.req.path,
      context.req.param("username"),
      "post",
    );
    if (activity == null) {
      return context.json({ error: "published create activity not found" }, 404);
    }
    return activityJsonResponse(buildPublishedCreateActivityJson(activity));
  });

  app.get("/users/:username/activities/create/comment/:activityId", async (context) => {
    const activity = await loadPublishedCreateActivityForRequest(
      config,
      context.req.path,
      context.req.param("username"),
      "comment",
    );
    if (activity == null) {
      return context.json({ error: "published create activity not found" }, 404);
    }
    return activityJsonResponse(buildPublishedCreateActivityJson(activity));
  });

  app.get("/users/:username/post/:objectId", async (context) => {
    const object = await loadPublishedObjectForRequest(config, context.req.path);
    if (object == null || object.kind !== "post") {
      return context.json({ error: "published post not found" }, 404);
    }
    return activityJsonResponse(buildPublishedActivityObjectJson(object));
  });

  app.get("/users/:username/comment/:objectId", async (context) => {
    const object = await loadPublishedObjectForRequest(config, context.req.path);
    if (object == null || object.kind !== "comment") {
      return context.json({ error: "published comment not found" }, 404);
    }
    return activityJsonResponse(buildPublishedActivityObjectJson(object));
  });

  app.get("/actors/:identifier/outbox", async (context) => {
    const identifier = context.req.param("identifier");
    if (!(await hasLocalActor(config, identifier))) {
      return context.json({ error: "actor not found" }, 404);
    }
    return activityJsonResponse(
      await buildEmptyOrderedCollection(
        new URL(`/actors/${identifier}/outbox`, config.fedifyOrigin),
      ).toJsonLd(),
    );
  });

  app.get("/actors/:identifier/followers", async (context) => {
    const identifier = context.req.param("identifier");
    if (!(await hasLocalActor(config, identifier))) {
      return context.json({ error: "actor not found" }, 404);
    }
    return activityJsonResponse(
      await buildEmptyOrderedCollection(
        new URL(`/actors/${identifier}/followers`, config.fedifyOrigin),
      ).toJsonLd(),
    );
  });

  app.post("/follow-community", async (context) => {
  // This endpoint exists as an operational bootstrap path until follow logic
  // is driven from the bot itself.
  if (
    !hasValidInternalAuthorization(
      config,
      context.req.header("Authorization") ?? null,
    )
  ) {
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
  if (
    !hasValidInternalAuthorization(
      config,
      context.req.header("Authorization") ?? null,
    )
  ) {
    return context.json({ error: "invalid authorization" }, { status: 401 });
  }
  const { communityActorUrl, followActivityId } = await context.req.json();

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

  try {
    await unfollowCommunity(fedify, config, communityActorUrl, followActivityId);
    return context.json({ ok: true });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return context.json({ error: message }, { status: 500 });
  }
});

app.post("/publish", async (context) => {
  // Python owns the Discord-side publish policy, while this endpoint owns the
  // signed user-actor Create delivery through Fedify.
  if (
    !hasValidInternalAuthorization(
      config,
      context.req.header("Authorization") ?? null,
    )
  ) {
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
      {
        error:
          "actorUsername, communityActorUrl, kind, and bodyMarkdown are required",
      },
      { status: 400 },
    );
  }
  if (payload.kind === "comment" && typeof payload.inReplyToObjectId !== "string") {
    return context.json(
      { error: "comment publish requires inReplyToObjectId" },
      { status: 400 },
    );
  }

  try {
    const result = await publishContent(fedify, config, {
      actorUsername: payload.actorUsername,
      communityActorUrl: payload.communityActorUrl,
      kind: payload.kind,
      title:
        typeof payload.title === "string" && payload.title.length > 0
          ? payload.title
          : null,
      bodyMarkdown: payload.bodyMarkdown,
      inReplyToObjectId:
        typeof payload.inReplyToObjectId === "string"
          ? payload.inReplyToObjectId
          : null,
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
  if (
    !hasValidInternalAuthorization(
      config,
      context.req.header("Authorization") ?? null,
    )
  ) {
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
      {
        error:
          "actorUsername, communityActorUrl, kind, and bodyMarkdown are required",
      },
      { status: 400 },
    );
  }
  if (payload.kind === "comment" && typeof payload.inReplyToObjectId !== "string") {
    return context.json(
      { error: "comment publish requires inReplyToObjectId" },
      { status: 400 },
    );
  }

  try {
    const result = await publishLocalCommunityContent(fedify, config, {
      actorUsername: payload.actorUsername,
      communityActorUrl: payload.communityActorUrl,
      kind: payload.kind,
      title:
        typeof payload.title === "string" && payload.title.length > 0
          ? payload.title
          : null,
      bodyMarkdown: payload.bodyMarkdown,
      inReplyToObjectId:
        typeof payload.inReplyToObjectId === "string"
          ? payload.inReplyToObjectId
          : null,
    });
    return context.json(result);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return context.json({ error: message }, { status: 500 });
  }
});


app.post("/send-local-community-relay", async (context) => {
  if (
    !hasValidInternalAuthorization(
      config,
      context.req.header("Authorization") ?? null,
    )
  ) {
    return context.json({ error: "invalid authorization" }, { status: 401 });
  }

  const payload = (await context.req.json()) as Partial<SendLocalCommunityRelayRequest>;
  if (typeof payload.signingActorUrl !== "string" || !Array.isArray(payload.deliveries)) {
    return context.json(
      { error: "signingActorUrl and deliveries are required" },
      { status: 400 },
    );
  }
  for (const delivery of payload.deliveries) {
    if (
      typeof delivery.deliveryId !== "number" ||
      typeof delivery.targetRemoteActorId !== "string" ||
      typeof delivery.targetInboxUrl !== "string" ||
      delivery.activityJson == null ||
      typeof delivery.activityJson !== "object" ||
      Array.isArray(delivery.activityJson)
    ) {
      return context.json(
        { error: "each delivery requires deliveryId, targetRemoteActorId, targetInboxUrl, and activityJson" },
        { status: 400 },
      );
    }
  }

  try {
    const result = await sendLocalCommunityRelay(fedify, config, {
      signingActorUrl: payload.signingActorUrl,
      deliveries: payload.deliveries,
    });
    return context.json(result);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return context.json({ error: message }, { status: 500 });
  }
});

app.post("/accept-local-community-follow", async (context) => {
  if (
    !hasValidInternalAuthorization(
      config,
      context.req.header("Authorization") ?? null,
    )
  ) {
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

app.post("/update", async (context) => {
  // Python owns the edit policy; this endpoint owns the signed Update delivery.
  if (
    !hasValidInternalAuthorization(
      config,
      context.req.header("Authorization") ?? null,
    )
  ) {
    return context.json({ error: "invalid authorization" }, { status: 401 });
  }

  const payload = (await context.req.json()) as Partial<UpdateContentRequest>;
  if (
    typeof payload.actorUsername !== "string" ||
    typeof payload.communityActorUrl !== "string" ||
    typeof payload.apObjectId !== "string" ||
    (payload.kind !== "post" && payload.kind !== "comment") ||
    typeof payload.bodyMarkdown !== "string"
  ) {
    return context.json(
      {
        error:
          "actorUsername, communityActorUrl, apObjectId, kind, and bodyMarkdown are required",
      },
      { status: 400 },
    );
  }

  try {
    await updateContent(fedify, config, {
      actorUsername: payload.actorUsername,
      communityActorUrl: payload.communityActorUrl,
      apObjectId: payload.apObjectId,
      kind: payload.kind,
      bodyMarkdown: payload.bodyMarkdown,
      title:
        typeof payload.title === "string" && payload.title.length > 0
          ? payload.title
          : null,
      inReplyToObjectId:
        typeof payload.inReplyToObjectId === "string" && payload.inReplyToObjectId.length > 0
          ? payload.inReplyToObjectId
          : null,
    });
    return context.json({ status: "ok" });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return context.json({ error: message }, { status: 500 });
  }
});

app.post("/delete", async (context) => {
  // Python owns the delete policy; this endpoint owns the signed Delete delivery.
  if (
    !hasValidInternalAuthorization(
      config,
      context.req.header("Authorization") ?? null,
    )
  ) {
    return context.json({ error: "invalid authorization" }, { status: 401 });
  }

  const payload = (await context.req.json()) as Partial<DeleteContentRequest>;
  if (
    typeof payload.actorUsername !== "string" ||
    typeof payload.communityActorUrl !== "string" ||
    typeof payload.apObjectId !== "string"
  ) {
    return context.json(
      { error: "actorUsername, communityActorUrl, and apObjectId are required" },
      { status: 400 },
    );
  }

  try {
    await deleteContent(fedify, config, {
      actorUsername: payload.actorUsername,
      communityActorUrl: payload.communityActorUrl,
      apObjectId: payload.apObjectId,
    });
    return context.json({ status: "ok" });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return context.json({ error: message }, { status: 500 });
  }
});

app.use(async (context, next) => {
  const method = context.req.method;
  const path = context.req.path;

  if (method === "POST" && path === "/inbox") {
    // Always log a compact inbox summary because federation debugging depends
    // on knowing whether the gateway saw Page/Create/Note traffic at all.
    console.log(`[HTTP] POST /inbox from ${context.req.header("user-agent")}`);
    const payloadSummary = await readInboxPayloadSummary(context);
    if (payloadSummary != null) {
      console.log("[HTTP] Inbox activity:", {
        type: payloadSummary.type,
        id: payloadSummary.id,
        objectType: payloadSummary.objectType,
        nestedObjectType: payloadSummary.nestedObjectType,
      });
      if (isDebug) {
        console.log("[HTTP][debug] Inbox payload summary:", payloadSummary);
        appendDebugFileLog("inbox.payload", {
          path,
          userAgent: context.req.header("user-agent") ?? null,
          signature: context.req.header("signature") ?? null,
          payloadSummary: payloadSummary as unknown as Record<string, unknown>,
        });
      }
    } else if (isDebug) {
      console.log("[HTTP][debug] Inbox payload summary failed");
    }
  }

  await next();

  if (isDebug && method === "POST" && path === "/inbox") {
    console.log(`[HTTP][debug] Response status: ${context.res.status}`);
    appendDebugFileLog("inbox.response", {
      path,
      status: context.res.status,
    });
  }
});

app.use(
  fedifyMiddleware(fedify, async (context): Promise<GatewayContextData> => {
    // The middleware attaches raw inbox data only where Announce recovery may
    // need it later in the Fedify processing pipeline.
    const activitypubRequestData = await buildActivityPubRequestData(context);
    return { ...config, ...activitypubRequestData };
  }),
);

  return app;
}

export function startGatewayServer(config: GatewayConfig = loadConfig()): void {
  const app = createGatewayApp(config);
  serve({
    fetch: app.fetch.bind(app),
    port: config.port,
  });

  console.log(
    `Fedify gateway listening on ${config.fedifyOrigin} (port ${config.port}) and forwarding to ${config.pythonBridgeEventsUrl}`,
  );
}

if (isMainModule()) {
  startGatewayServer();
}

function isMainModule(): boolean {
  const entry = process.argv[1];
  return entry != null && import.meta.url === pathToFileURL(entry).href;
}

async function buildRegisteredUserActorDocument(
  config: GatewayConfig,
  fedify: ReturnType<typeof createGatewayFederation>,
  username: string,
) {
  const userIdentity = await loadUserActorIdentity(config, username);
  if (userIdentity == null) {
    return null;
  }
  const context = fedify.createContext(new URL(config.fedifyOrigin), config);
  return buildUserPersonActor(
    userIdentity,
    new URL("/inbox", config.fedifyOrigin),
    await context.getActorKeyPairs(username),
  );
}

async function buildLocalCommunityActorDocument(
  config: GatewayConfig,
  fedify: ReturnType<typeof createGatewayFederation>,
  slug: string,
) {
  const communityIdentity = await loadLocalCommunityIdentity(config, slug);
  if (communityIdentity == null) {
    return null;
  }
  return buildLocalCommunityGroupActor(
    communityIdentity,
    new URL("/inbox", config.fedifyOrigin),
    await buildLocalCommunityPublicKeyCarrier(communityIdentity),
  );
}

function activityJsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: {
      "Content-Type": "application/activity+json",
    },
  });
}

async function loadPublishedObjectForRequest(
  config: GatewayConfig,
  requestPath: string,
) {
  // Route lookup uses the exact canonical object URL, which keeps restart
  // behavior deterministic and avoids rebuilding object IDs from fragments.
  const objectUrl = new URL(requestPath, config.fedifyOrigin).href;
  return await loadPublishedActivityObjectByObjectId(config, objectUrl);
}


async function loadPublishedCreateActivityForRequest(
  config: GatewayConfig,
  requestPath: string,
  username: string,
  kind: "post" | "comment",
) {
  // The delivered Create.id is a full URL. Reconstruct it from the request path
  // so dereference uses the exact same durable id Python stored after publish.
  const activityUrl = new URL(requestPath, config.fedifyOrigin).href;
  const activity = await loadPublishedActivityObjectByActivityId(config, activityUrl);
  if (
    activity == null ||
    activity.actorUsername !== username ||
    activity.kind !== kind
  ) {
    return null;
  }
  return activity;
}

function hasValidInternalAuthorization(
  config: GatewayConfig,
  authorizationHeader: string | null,
): boolean {
  return (
    authorizationHeader
    === `Bearer ${config.pythonBridgeSharedSecret}`
  );
}

interface InboxPayloadSummary {
  type: string | null;
  id: string | null;
  objectType: string | null;
  objectId: string | null;
  nestedObjectType: string | null;
  nestedObjectId: string | null;
  rawParsed: Record<string, unknown>;
}

async function readInboxPayloadSummary(
  context: RawRequestContext,
): Promise<InboxPayloadSummary | null> {
  try {
    const rawBody = await context.req.raw.clone().text();
    const parsed = JSON.parse(rawBody) as Record<string, unknown>;
    return { ...summarizeInboxPayload(parsed), rawParsed: parsed };
  } catch {
    return null;
  }
}

async function buildActivityPubRequestData(
  context: RawRequestContext,
): Promise<Pick<GatewayContextData, "activitypubRawJson" | "activitypubRawBodySha256">> {
  if (!isInboxPost(context.req.raw.method, context.req.raw.url)) {
    return {};
  }

  try {
    const rawBody = await context.req.raw.clone().text();
    const activitypubRawJson = JSON.parse(rawBody);
    const activitypubRawBodySha256 = createHash("sha256")
      .update(rawBody)
      .digest("hex");

    if (hasStringId(activitypubRawJson)) {
      // Cache by activity id so queued Announce handling can recover the exact
      // payload that originally hit /inbox.
      storeRawActivity(
        activitypubRawJson.id,
        activitypubRawJson,
        activitypubRawBodySha256,
      );
    }

    return {
      activitypubRawJson,
      activitypubRawBodySha256,
    };
  } catch {
    return {};
  }
}

function isInboxPost(method: string, url: string): boolean {
  return method === "POST" && new URL(url).pathname === "/inbox";
}

function summarizeInboxPayload(
  parsed: Record<string, unknown>,
): Omit<InboxPayloadSummary, "rawParsed"> {
  // The summary intentionally keeps only the fields needed to distinguish
  // post/comment and wrapped/unwrapped ActivityPub shapes in logs.
  const object = asRecord(parsed.object);
  const nestedObject = asRecord(object?.object);
  return {
    type: asString(parsed.type),
    id: asString(parsed.id),
    objectType: asString(object?.type),
    objectId: asString(object?.id),
    nestedObjectType: asString(nestedObject?.type),
    nestedObjectId: asString(nestedObject?.id),
  };
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function hasStringId(
  value: unknown,
): value is Record<"id", string> & Record<string, unknown> {
  return (
    typeof value === "object" &&
    value !== null &&
    "id" in value &&
    typeof value.id === "string"
  );
}

interface RawRequestContext {
  req: {
    raw: Request;
  };
}
