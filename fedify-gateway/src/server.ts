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

  if (method === "POST" && path === "/inbox") {
    console.log(`[HTTP] Response status: ${context.res.status}`);
  }
});

app.use(
  fedifyMiddleware(fedify, async (context): Promise<GatewayContextData> => {
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

interface InboxPayloadSummary {
  type: string | null;
  id: string | null;
  objectType: string | null;
  objectId: string | null;
  nestedObjectType: string | null;
  nestedObjectId: string | null;
}

async function readInboxPayloadSummary(
  context: RawRequestContext,
): Promise<InboxPayloadSummary | null> {
  try {
    const rawBody = await context.req.raw.clone().text();
    const parsed = JSON.parse(rawBody) as Record<string, unknown>;
    return summarizeInboxPayload(parsed);
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
): InboxPayloadSummary {
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
