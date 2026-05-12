import { createHash } from "node:crypto";

import { serve } from "@hono/node-server";
import { federation as fedifyMiddleware } from "@fedify/hono";
import { Hono } from "hono";

import { storeRawActivity } from "./activitypub-raw-cache.js";
import {
  buildEmptyOrderedCollection,
  buildUserPersonActor,
} from "./actors.js";
import {
  getBridgeActorIdentity,
  hasLocalActor,
  loadUserActorIdentity,
} from "./actor-store.js";
import { type GatewayContextData, loadConfig } from "./config.js";
import { loadPublishedActivityObjectByObjectId } from "./db.js";
import { createGatewayFederation } from "./federation.js";
import {
  deleteContent,
  followCommunity,
  publishContent,
  unfollowCommunity,
  updateContent,
} from "./federation-outbound.js";
import { buildPublishedActivityObjectJson } from "./published-objects.js";
import type {
  DeleteContentRequest,
  PublishContentRequest,
  UpdateContentRequest,
} from "./types.js";

// server.ts owns the operator-facing HTTP surface of the gateway: health,
// manual follow, inbox logging, and Fedify middleware wiring.
const config = loadConfig();
const fedify = createGatewayFederation(config);

const app = new Hono();
const isDebug = config.logLevel === "debug";

app.get("/healthz", (context) => {
  return context.json({ status: "ok" });
});

app.get("/.well-known/webfinger", async (context) => {
  const resource = context.req.query("resource");
  if (!resource) {
    return context.json({ error: "resource query parameter is required" }, 400);
  }

  const webFingerDocument = await buildWebFingerDocument(resource);
  if (webFingerDocument == null) {
    return context.json({ error: "resource not found" }, 404);
  }
  return context.newResponse(JSON.stringify(webFingerDocument), 200, {
    "Content-Type": "application/jrd+json",
  });
});

app.get("/users/:username", async (context) => {
  const username = context.req.param("username");
  const actor = await buildRegisteredUserActorDocument(username);
  if (actor == null) {
    return context.json({ error: "user actor not found" }, 404);
  }
  return activityJsonResponse(await actor.toJsonLd());
});

app.get("/users/:username/outbox", async (context) => {
  const username = context.req.param("username");
  if (!(await hasLocalActor(config, username))) {
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
  if (!(await hasLocalActor(config, username))) {
    return context.json({ error: "user actor not found" }, 404);
  }
  return activityJsonResponse(
    await buildEmptyOrderedCollection(
      new URL(`/users/${username}/followers`, config.fedifyOrigin),
    ).toJsonLd(),
  );
});

app.get("/users/:username/post/:objectId", async (context) => {
  const object = await loadPublishedObjectForRequest(context.req.path);
  if (object == null || object.kind !== "post") {
    return context.json({ error: "published post not found" }, 404);
  }
  return activityJsonResponse(buildPublishedActivityObjectJson(object));
});

app.get("/users/:username/comment/:objectId", async (context) => {
  const object = await loadPublishedObjectForRequest(context.req.path);
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

app.post("/update", async (context) => {
  // Python owns the edit policy; this endpoint owns the signed Update delivery.
  if (
    !hasValidInternalAuthorization(
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
      }
    } else if (isDebug) {
      console.log("[HTTP][debug] Inbox payload summary failed");
    }
  }

  await next();

  if (isDebug && method === "POST" && path === "/inbox") {
    console.log(`[HTTP][debug] Response status: ${context.res.status}`);
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

serve({
  fetch: app.fetch.bind(app),
  port: config.port,
});

console.log(
  `Fedify gateway listening on ${config.fedifyOrigin} (port ${config.port}) and forwarding to ${config.pythonBridgeEventsUrl}`,
);

async function buildWebFingerDocument(
  resource: string,
): Promise<Record<string, unknown> | null> {
  // The manual WebFinger route preserves the canonical `/users/{username}`
  // surface even though Fedify itself only supports one actor dispatcher path.
  const localHost = new URL(config.fedifyOrigin).host;
  const bridgeIdentity = getBridgeActorIdentity(config);

  if (resource === `acct:${config.actorIdentifier}@${localHost}`) {
    return {
      subject: resource,
      aliases: [bridgeIdentity.actorId.href],
      links: [
        {
          rel: "self",
          type: "application/activity+json",
          href: bridgeIdentity.actorId.href,
        },
      ],
    };
  }

  const username = parseLocalAcctResource(resource, localHost);
  if (username == null) {
    return null;
  }

  const userIdentity = await loadUserActorIdentity(config, username);
  if (userIdentity == null) {
    return null;
  }

  return {
    subject: resource,
    aliases: [userIdentity.actorId.href],
    links: [
      {
        rel: "self",
        type: "application/activity+json",
        href: userIdentity.actorId.href,
      },
    ],
  };
}

async function buildRegisteredUserActorDocument(username: string) {
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

function parseLocalAcctResource(
  resource: string,
  localHost: string,
): string | null {
  if (!resource.startsWith("acct:")) {
    return null;
  }
  const handle = resource.slice("acct:".length);
  const parts = handle.split("@");
  if (parts.length !== 2 || parts[0].length === 0 || parts[1] !== localHost) {
    return null;
  }
  return parts[0];
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
  requestPath: string,
) {
  // Route lookup uses the exact canonical object URL, which keeps restart
  // behavior deterministic and avoids rebuilding object IDs from fragments.
  const objectUrl = new URL(requestPath, config.fedifyOrigin).href;
  return await loadPublishedActivityObjectByObjectId(config, objectUrl);
}

function hasValidInternalAuthorization(
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
