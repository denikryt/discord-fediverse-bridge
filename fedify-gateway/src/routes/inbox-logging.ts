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
import { parseTypedActorIdentifier, type TypedActorIdentifier } from "../typed-actor-routing.js";

export interface InboxLoggingDependencies {
  isDebug: boolean;
}

export interface ActivityPubRequestData {
  activitypubRawJson?: unknown;
  activitypubRawBodySha256?: string;
}

export type InboxPathClassification =
  | { kind: "shared"; path: "/inbox" }
  | { kind: "actor"; actor: TypedActorIdentifier; path: string };

/** Register compact /inbox logging immediately before Fedify middleware. */
export function registerInboxLoggingMiddleware(
  app: Hono,
  dependencies: InboxLoggingDependencies,
): void {
  app.use(async (context, next) => {
    const method = context.req.method;
    const path = context.req.path;
    const classification = classifyInboxPath(method, path);

    if (classification != null) {
      // Always log a compact inbox summary because federation debugging depends
      // on knowing whether the gateway saw Page/Create/Note traffic at all.
      console.log(`[HTTP] POST ${classification.path} from ${context.req.header("user-agent")}`);
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
            path: classification.path,
            inboxKind: classification.kind,
            actorKind: classification.kind === "actor" ? classification.actor.kind : null,
            actorIdentifier: classification.kind === "actor" ? classification.actor.identifier : null,
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

    if (dependencies.isDebug && classification != null) {
      console.log(`[HTTP][debug] Response status: ${context.res.status}`);
      appendDebugFileLog("inbox.response", {
        path: classification.path,
        inboxKind: classification.kind,
        status: context.res.status,
      });
    }
  });
}

/** Build Fedify context data for raw inbound ActivityPub recovery. */
export async function buildActivityPubRequestData(
  context: RawRequestContext,
): Promise<ActivityPubRequestData> {
  if (classifyInboxPath(context.req.raw.method, context.req.raw.url) == null) {
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

/** Classify shared and future typed actor inbox POST paths for raw capture. */
export function classifyInboxPath(
  method: string,
  url: string,
): InboxPathClassification | null {
  if (method !== "POST") {
    return null;
  }

  let path: string;
  try {
    path = new URL(url, "http://activitypub.local").pathname;
  } catch {
    return null;
  }

  if (path === "/inbox") {
    return { kind: "shared", path };
  }

  const prefix = "/actors/";
  const suffix = "/inbox";
  if (!path.startsWith(prefix) || !path.endsWith(suffix)) {
    return null;
  }

  const tail = path.slice(prefix.length, -suffix.length);
  const actor = parseTypedActorIdentifier(tail);
  if (actor == null) {
    return null;
  }
  return { kind: "actor", actor, path };
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
