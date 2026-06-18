/**
 * Inbox pre-Fedify logging and raw ActivityPub request capture.
 *
 * The middleware intentionally receives only the debug flag. Raw inbox body
 * capture is public request handling and must not need process secrets.
 */
import { createHash } from "node:crypto";

import type { Hono } from "hono";

import { storeRawActivity } from "../activitypub-raw-cache.js";
import { appendDebugFileLog } from "../debug-file-log.js";

export interface InboxLoggingDependencies {
  isDebug: boolean;
}

export interface ActivityPubRequestData {
  activitypubRawJson?: unknown;
  activitypubRawBodySha256?: string;
}

/** Register compact /inbox logging immediately before Fedify middleware. */
export function registerInboxLoggingMiddleware(
  app: Hono,
  dependencies: InboxLoggingDependencies,
): void {
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
        if (dependencies.isDebug) {
          console.log("[HTTP][debug] Inbox payload summary:", payloadSummary);
          appendDebugFileLog("inbox.payload", {
            path,
            userAgent: context.req.header("user-agent") ?? null,
            signature: context.req.header("signature") ?? null,
            payloadSummary: payloadSummary as unknown as Record<string, unknown>,
          });
        }
      } else if (dependencies.isDebug) {
        console.log("[HTTP][debug] Inbox payload summary failed");
      }
    }

    await next();

    if (dependencies.isDebug && method === "POST" && path === "/inbox") {
      console.log(`[HTTP][debug] Response status: ${context.res.status}`);
      appendDebugFileLog("inbox.response", {
        path,
        status: context.res.status,
      });
    }
  });
}

/** Build Fedify context data for raw inbound ActivityPub recovery. */
export async function buildActivityPubRequestData(
  context: RawRequestContext,
): Promise<ActivityPubRequestData> {
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
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function hasStringId(
  value: unknown,
): value is Record<"id", string> & Record<string, unknown> {
  return typeof value === "object" && value !== null && "id" in value && typeof value.id === "string";
}

interface RawRequestContext {
  req: {
    raw: Request;
  };
}
