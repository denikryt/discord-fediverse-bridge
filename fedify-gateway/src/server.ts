import { createHash } from "node:crypto";

import { serve } from "@hono/node-server";
import { federation as fedifyMiddleware } from "@fedify/hono";
import { Hono } from "hono";

import { storeRawActivity } from "./activitypub-raw-cache.js";
import { type GatewayContextData, loadConfig } from "./config.js";
import { createGatewayFederation } from "./federation.js";
import { FileKeyStore } from "./key-store.js";
import { followCommunity } from "./federation-outbound.js";

const config = loadConfig();
const keyStore = new FileKeyStore(config.keyStorePath);
const fedify = createGatewayFederation(config, keyStore);

const app = new Hono();
const isDebug = config.logLevel === "debug";

app.get("/healthz", (context) => {
  return context.json({ status: "ok" });
});

app.post("/follow-community", async (context) => {
  const { communityActorUrl } = await context.req.json();

  if (!communityActorUrl || typeof communityActorUrl !== "string") {
    return context.json(
      { error: "communityActorUrl is required and must be a string" },
      { status: 400 },
    );
  }

  try {
    await followCommunity(fedify, config, communityActorUrl);
    return context.json({ success: true });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return context.json({ error: message }, { status: 500 });
  }
});

app.use(async (context, next) => {
  const method = context.req.method;
  const path = context.req.path;

  if (method === "POST" && path === "/inbox") {
    console.log(`[HTTP] POST /inbox from ${context.req.header("user-agent")}`);
    try {
      const rawBody = await context.req.raw.clone().text();
      const parsed = JSON.parse(rawBody) as Record<string, unknown>;
      const object =
        typeof parsed.object === "object" && parsed.object !== null
          ? (parsed.object as Record<string, unknown>)
          : null;
      const nestedObject =
        object && typeof object.object === "object" && object.object !== null
          ? (object.object as Record<string, unknown>)
          : null;
      console.log("[HTTP] Inbox activity:", {
        type: typeof parsed.type === "string" ? parsed.type : null,
        id: typeof parsed.id === "string" ? parsed.id : null,
        objectType: object && typeof object.type === "string" ? object.type : null,
        nestedObjectType:
          nestedObject && typeof nestedObject.type === "string"
            ? nestedObject.type
            : null,
      });
    } catch {
      // Keep request path resilient even if body parsing fails.
    }
    if (isDebug) {
      try {
        const rawBody = await context.req.raw.clone().text();
        const parsed = JSON.parse(rawBody) as Record<string, unknown>;
        const object =
          typeof parsed.object === "object" && parsed.object !== null
            ? (parsed.object as Record<string, unknown>)
            : null;
        const nestedObject =
          object && typeof object.object === "object" && object.object !== null
            ? (object.object as Record<string, unknown>)
            : null;
        console.log("[HTTP][debug] Inbox payload summary:", {
          type: typeof parsed.type === "string" ? parsed.type : null,
          id: typeof parsed.id === "string" ? parsed.id : null,
          objectType: object && typeof object.type === "string" ? object.type : null,
          objectId: object && typeof object.id === "string" ? object.id : null,
          nestedObjectType:
            nestedObject && typeof nestedObject.type === "string"
              ? nestedObject.type
              : null,
          nestedObjectId:
            nestedObject && typeof nestedObject.id === "string"
              ? nestedObject.id
              : null,
        });
      } catch (error) {
        console.log(
          "[HTTP][debug] Inbox payload summary failed:",
          error instanceof Error ? error.message : String(error),
        );
      }
    }
  }

  await next();

  if (method === "POST" && path === "/inbox") {
    console.log(`[HTTP] Response status: ${context.res.status}`);
  }
});

app.use(
  fedifyMiddleware(fedify, async (context): Promise<GatewayContextData> => {
    let activitypubRawJson: unknown;
    let activitypubRawBodySha256: string | undefined;
    const method = context.req.raw.method;
    const pathname = new URL(context.req.raw.url).pathname;

    if (method === "POST" && pathname === "/inbox") {
      try {
        const rawBody = await context.req.raw.clone().text();
        activitypubRawBodySha256 = createHash("sha256").update(rawBody).digest("hex");
        activitypubRawJson = JSON.parse(rawBody);
        if (
          typeof activitypubRawJson === "object" &&
          activitypubRawJson !== null &&
          "id" in activitypubRawJson &&
          typeof activitypubRawJson.id === "string"
        ) {
          storeRawActivity(
            activitypubRawJson.id,
            activitypubRawJson,
            activitypubRawBodySha256,
          );
        }
      } catch {
        activitypubRawJson = undefined;
      }
    }

    return {
      ...config,
      activitypubRawJson,
      activitypubRawBodySha256,
    };
  }),
);

serve({
  fetch: app.fetch.bind(app),
  port: config.port,
});

console.log(
  `Fedify gateway listening on ${config.fedifyOrigin} (port ${config.port}) and forwarding to ${config.pythonBridgeEventsUrl}`,
);
