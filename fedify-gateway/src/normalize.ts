import { randomUUID } from "node:crypto";

import { Create, Link } from "@fedify/vocab";
import { Note } from "@fedify/vocab";
import { Page } from "@fedify/vocab";
import { type Object as ActivityObject } from "@fedify/vocab";

import { loadPublishedActivityObjectByObjectIdForDatabaseUrl } from "./db.js";
import type { BridgeEvent } from "./types.js";

export async function normalizeCreateActivity(
  activity: Create,
): Promise<BridgeEvent | null> {
  // Typed Fedify objects are preferred when available because they already
  // resolve some vocabulary details for us.
  const object = await activity.getObject({ suppressError: true });
  if (object == null) {
    return null;
  }

  if (object instanceof Page) {
    return await normalizePostActivity(activity, object);
  }

  if (object instanceof Note) {
    return await normalizeCommentActivity(activity, object);
  }

  return null;
}

export async function normalizeCreateActivityFromJson(
  activity: unknown,
): Promise<BridgeEvent | null> {
  // Raw JSON normalization is the fallback for wrapped Announce payloads where
  // the typed Fedify object path is not reliable enough for nested Create.
  if (!isRecord(activity) || activity.type !== "Create") {
    return null;
  }

  const object = asRecord(activity.object);
  if (!object) {
    return null;
  }

  if (object.type === "Page" || object.type === "Article") {
    return normalizePostActivityFromJson(activity, object);
  }

  if (object.type === "Note") {
    return await normalizeCommentActivityFromJson(activity, object);
  }

  return null;
}

export async function normalizeUpdateActivityFromJson(
  activity: unknown,
): Promise<BridgeEvent | null> {
  // Handles the unwrapped Update record extracted from Announce(Update(...)).
  // Delegates to the same sub-normalizers as Create but overrides event_type.
  if (!isRecord(activity) || activity.type !== "Update") {
    return null;
  }

  const object = asRecord(activity.object);
  if (!object) {
    return null;
  }

  if (object.type === "Page" || object.type === "Article") {
    const event = normalizePostActivityFromJson(activity, object);
    // Override event_type: the object shape is identical to Create but this is an edit.
    return { ...event, event_type: "post.updated" };
  }

  if (object.type === "Note") {
    const event = await normalizeCommentActivityFromJson(activity, object);
    // Override event_type: same shape as Create but this is an edit.
    return { ...event, event_type: "comment.updated" };
  }

  return null;
}

export function normalizeDeleteActivityFromJson(
  activity: unknown,
): BridgeEvent | null {
  // Handles the unwrapped Delete record extracted from Announce(Delete(...)).
  // Lemmy 0.19 sends object as a plain string URL (primary path).
  // Older format fallback: object may be { id: "..." }.
  if (!isRecord(activity) || activity.type !== "Delete") {
    return null;
  }

  // Primary path: object is a plain string URL (confirmed from real Lemmy 0.19.18 logs).
  // Fallback: object is a record with an id field (older Lemmy versions).
  const apId =
    asString(activity.object) ??
    asString(asRecord(activity.object)?.id);

  if (!apId) {
    return null;
  }

  // community_actor_id lives directly on the Delete record's audience field —
  // confirmed from real logs. object is a plain string so we can't read it from there.
  // Fall back to cc[0] if audience is absent.
  const communityActorId =
    resolveCommunityActorIdForDelete(activity) ??
    asString(activity.actor) ??
    "";

  // Infer kind from the URL path: /post/ → post, /comment/ → comment.
  const kind = inferKindFromApId(apId);
  if (!kind) {
    return null;
  }

  const eventType = kind === "post" ? "post.deleted" : "comment.deleted";
  const now = new Date().toISOString();

  return {
    actor_id: asString(activity.actor) ?? "",
    community_actor_id: communityActorId,
    delivery_id: asString(activity.id) ?? randomUUID(),
    event_type: eventType,
    occurred_at: now,
    object: {
      ap_id: apId,
      // author_name, title, body_markdown are irrelevant for delete handlers —
      // they only look up the record by ap_id. Stubs satisfy the required schema.
      author_name: "",
      body_markdown: null,
      kind,
      // lemmy_id: 0 is a safe stub — delete handlers never read this field.
      lemmy_id: parseLemmyNumericIdOrZero(apId, kind),
      parent_ap_id: null,
      post_ap_id: null,
      post_lemmy_id: null,
      // published_at must be a valid ISO string for Pydantic; now() satisfies that.
      published_at: now,
      title: null,
      url: apId,
    },
  };
}

async function normalizePostActivity(
  activity: Create,
  object: Page,
): Promise<BridgeEvent> {
  const apId = requireUrl(object.id, "post object id");
  // Lemmy link posts store the external article URL in attachment[0].href.
  // object.url points back to the Lemmy post itself, so prefer attachment
  // when it differs from the AP id.
  const url = await resolvePostUrl(object, apId);
  const publishedAt = toIsoString(object.published) ?? toIsoString(activity.published);

  return {
    actor_id: activity.actorId?.href ?? "",
    community_actor_id: resolveCommunityActorId(object),
    delivery_id: activity.id?.href ?? randomUUID(),
    event_type: "post.created",
    occurred_at: publishedAt,
    object: {
      ap_id: apId,
      author_name: await resolveAuthorName(activity),
      body_markdown: resolveMarkdownBody(object),
      kind: "post",
      lemmy_id: parseRequiredLemmyNumericId(apId, "post"),
      parent_ap_id: null,
      post_ap_id: null,
      post_lemmy_id: null,
      published_at: publishedAt,
      title: resolveText(object.name),
      url,
    },
  };
}

async function normalizeCommentActivity(
  activity: Create,
  object: Note,
): Promise<BridgeEvent> {
  const apId = requireUrl(object.id, "comment object id");
  const replyTargetId = object.replyTargetId?.href;
  if (!replyTargetId) {
    throw new Error(`Comment ${apId} is missing inReplyTo/replyTarget`);
  }

  const replyContext = await resolveReplyChainContext(replyTargetId);
  if (!replyContext.postApId) {
    throw new Error(`Could not resolve post AP ID for comment ${apId}`);
  }

  const url = resolveObjectUrl(object) ?? apId;
  const publishedAt = toIsoString(object.published) ?? toIsoString(activity.published);

  return {
    actor_id: activity.actorId?.href ?? "",
    community_actor_id: resolveCommunityActorId(object),
    delivery_id: activity.id?.href ?? randomUUID(),
    event_type: "comment.created",
    occurred_at: publishedAt,
    object: {
      ap_id: apId,
      author_name: await resolveAuthorName(activity),
      body_markdown: resolveMarkdownBody(object),
      kind: "comment",
      lemmy_id: parseRequiredLemmyNumericId(apId, "comment"),
      parent_ap_id: replyContext.parentApId,
      post_ap_id: replyContext.postApId,
      post_lemmy_id: parseReplyTargetPostNumericId(
        replyContext.postApId,
        replyContext.postSource,
      ),
      published_at: publishedAt,
      title: null,
      url,
    },
  };
}

async function resolveAuthorName(activity: Create): Promise<string> {
  const actor = await activity.getActor({ suppressError: true });
  const preferred = actor?.preferredUsername;
  if (typeof preferred === "string" && preferred.length > 0) {
    return preferred;
  }
  const name = actor?.name;
  if (typeof name === "string" && name.length > 0) {
    return name;
  }
  const actorId = activity.actorId?.href;
  if (actorId) {
    return lastPathSegment(actorId);
  }
  return "unknown";
}

function resolveCommunityActorId(object: ActivityObject): string {
  const candidates = [
    object.audienceId?.href,
    ...object.toIds.map((id) => id.href),
    ...object.ccIds.map((id) => id.href),
  ].filter((value): value is string => Boolean(value));

  const community = candidates.find((candidate) => isCommunityActorId(candidate));
  if (!community) {
    throw new Error("Could not resolve community actor id from ActivityPub object");
  }
  return community;
}

function resolveMarkdownBody(object: ActivityObject): string | null {
  const sourceContent = object.source?.content;
  if (typeof sourceContent === "string" && sourceContent.length > 0) {
    return sourceContent;
  }
  if (typeof object.content === "string" && object.content.length > 0) {
    return object.content;
  }
  return null;
}

function normalizePostActivityFromJson(
  activity: Record<string, unknown>,
  object: Record<string, unknown>,
): BridgeEvent {
  const apId = requireString(object.id, "post object id");
  const publishedAt = asString(object.published) ?? asString(activity.published) ?? new Date().toISOString();
  // Lemmy link posts store the external article URL in attachment[0].href.
  // object.url points back to the Lemmy post itself, so prefer attachment
  // when it differs from the AP id.
  const url = resolvePostUrlFromJson(object, apId);

  return {
    actor_id: asString(activity.actor) ?? "",
    community_actor_id: resolveCommunityActorIdFromJson(object),
    delivery_id: asString(activity.id) ?? randomUUID(),
    event_type: "post.created",
    occurred_at: publishedAt,
    object: {
      ap_id: apId,
      author_name: resolveAuthorNameFromJson(activity, object),
      body_markdown: resolveMarkdownBodyFromJson(object),
      kind: "post",
      lemmy_id: parseRequiredLemmyNumericId(apId, "post"),
      parent_ap_id: null,
      post_ap_id: null,
      post_lemmy_id: null,
      published_at: publishedAt,
      title: asString(object.name),
      url,
    },
  };
}

function resolvePostUrlFromJson(object: Record<string, unknown>, apId: string): string {
  // Check attachment array first for the external article URL.
  const attachments = object.attachment;
  if (Array.isArray(attachments)) {
    for (const a of attachments) {
      const href = asString((a as Record<string, unknown>).href);
      if (href && href !== apId) {
        return href;
      }
    }
  }
  return asString(object.url) ?? apId;
}

async function normalizeCommentActivityFromJson(
  activity: Record<string, unknown>,
  object: Record<string, unknown>,
): Promise<BridgeEvent> {
  const apId = requireString(object.id, "comment object id");
  const replyTarget = asString(object.inReplyTo) ?? asString(object.replyTarget);
  if (!replyTarget) {
    throw new Error(`Comment ${apId} is missing inReplyTo`);
  }

  const replyContext = await resolveReplyChainContext(replyTarget);
  if (!replyContext.postApId) {
    throw new Error(`Could not resolve post AP ID for comment ${apId}`);
  }

  const publishedAt = asString(object.published) ?? asString(activity.published) ?? new Date().toISOString();

  return {
    actor_id: asString(activity.actor) ?? "",
    community_actor_id: resolveCommunityActorIdFromJson(object),
    delivery_id: asString(activity.id) ?? randomUUID(),
    event_type: "comment.created",
    occurred_at: publishedAt,
    object: {
      ap_id: apId,
      author_name: resolveAuthorNameFromJson(activity, object),
      body_markdown: resolveMarkdownBodyFromJson(object),
      kind: "comment",
      lemmy_id: parseRequiredLemmyNumericId(apId, "comment"),
      parent_ap_id: replyContext.parentApId,
      post_ap_id: replyContext.postApId,
      post_lemmy_id: parseReplyTargetPostNumericId(
        replyContext.postApId,
        replyContext.postSource,
      ),
      published_at: publishedAt,
      title: null,
      url: asString(object.url) ?? apId,
    },
  };
}

function resolveCommunityActorIdFromJson(object: Record<string, unknown>): string {
  const candidates = [
    ...normalizeStringArray(object.audience),
    ...normalizeStringArray(object.to),
    ...normalizeStringArray(object.cc),
  ];

  const community = candidates.find((candidate) => isCommunityActorId(candidate));
  if (!community) {
    throw new Error("Could not resolve community actor id from raw ActivityPub object");
  }
  return community;
}

function resolveMarkdownBodyFromJson(object: Record<string, unknown>): string | null {
  const source = asRecord(object.source);
  const sourceContent = asString(source?.content);
  if (sourceContent) {
    return sourceContent;
  }
  return asString(object.content);
}

function resolveAuthorNameFromJson(
  activity: Record<string, unknown>,
  object: Record<string, unknown>,
): string {
  const actorId = asString(activity.actor) ?? asString(object.attributedTo);
  if (!actorId) {
    return "unknown";
  }
  return lastPathSegment(actorId);
}

async function resolveReplyChainContext(
  replyTarget: string,
  visited: Set<string> = new Set(),
): Promise<{
  postApId: string | null;
  parentApId: string | null;
  postSource: "local" | "remote";
}> {
  // Guard against cycles.
  if (visited.has(replyTarget)) {
    return { postApId: null, parentApId: null, postSource: "remote" };
  }
  visited.add(replyTarget);

  const storedObject = await loadStoredActivityObject(replyTarget);
  if (storedObject != null) {
    if (storedObject.kind === "post") {
      return {
        postApId: storedObject.objectId,
        parentApId: null,
        postSource: "local",
      };
    }
    if (!storedObject.inReplyToObjectId) {
      return {
        postApId: null,
        parentApId: storedObject.objectId,
        postSource: "local",
      };
    }
    const nestedContext = await resolveReplyChainContext(
      storedObject.inReplyToObjectId,
      visited,
    );
    return {
      postApId: nestedContext.postApId,
      parentApId: storedObject.objectId,
      postSource: nestedContext.postSource,
    };
  }

  // Fast path: Lemmy post URL — no fetch needed once local object lookup has
  // already had the chance to claim gateway-owned paths first.
  if (isLemmyPath(replyTarget, "post")) {
    return { postApId: replyTarget, parentApId: null, postSource: "remote" };
  }

  // Remote fallback keeps reply resolution working for non-local objects that
  // are not stored in our own shared database.
  const parentRecord = await fetchActivityObject(replyTarget);
  if (parentRecord == null) {
    return { postApId: null, parentApId: null, postSource: "remote" };
  }

  if (parentRecord.type === "Page" || parentRecord.type === "Article") {
    return {
      postApId: asString(parentRecord.id),
      parentApId: null,
      postSource: "remote",
    };
  }

  const nextReplyTarget =
    asString(parentRecord.inReplyTo) ?? asString(parentRecord.replyTarget);
  if (!nextReplyTarget) {
    return {
      postApId: null,
      parentApId: isCommentLikeReplyTarget(replyTarget, parentRecord)
        ? replyTarget
        : null,
      postSource: "remote",
    };
  }
  const nestedContext = await resolveReplyChainContext(nextReplyTarget, visited);
  return {
    postApId: nestedContext.postApId,
    parentApId: isCommentLikeReplyTarget(replyTarget, parentRecord)
      ? replyTarget
      : nestedContext.parentApId,
    postSource: nestedContext.postSource,
  };
}

async function resolvePostUrl(object: Page, apId: string): Promise<string> {
  // Check attachment first: Lemmy link posts place the external article URL
  // in attachment[0].href. Only use it when it differs from the AP id so
  // text-only posts (which have no meaningful attachment) still fall back
  // to the Lemmy post URL.
  for await (const attachment of object.getAttachments()) {
    if (!(attachment instanceof Link)) continue;
    const href = attachment.href?.href;
    if (href && href !== apId) {
      return href;
    }
  }
  return resolveObjectUrl(object) ?? apId;
}

function resolveObjectUrl(object: ActivityObject): string | null {
  const value = object.url;
  if (value instanceof URL) {
    return value.href;
  }
  if (value && "href" in value && value.href instanceof URL) {
    return value.href.href;
  }
  return null;
}

function resolveText(value: string | { toString(): string } | null | undefined): string | null {
  if (typeof value === "string") {
    return value;
  }
  if (value == null) {
    return null;
  }
  return value.toString();
}

function requireUrl(value: URL | null, label: string): string {
  if (!value) {
    throw new Error(`Missing ${label}`);
  }
  return value.href;
}

function requireString(value: unknown, label: string): string {
  const resolved = asString(value);
  if (!resolved) {
    throw new Error(`Missing ${label}`);
  }
  return resolved;
}

function asString(value: unknown): string | null {
  if (typeof value === "string" && value.length > 0) {
    return value;
  }
  return null;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!isRecord(value)) {
    return null;
  }
  return value;
}

function normalizeStringArray(value: unknown): string[] {
  if (typeof value === "string") {
    return [value];
  }
  if (Array.isArray(value)) {
    return value.flatMap((item) => (typeof item === "string" ? [item] : []));
  }
  return [];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

async function fetchActivityObject(
  objectId: string,
): Promise<Record<string, unknown> | null> {
  try {
    const response = await fetch(objectId, {
      headers: {
        Accept: "application/activity+json",
      },
    });
    if (!response.ok) {
      return null;
    }
    const payload = (await response.json()) as unknown;
    return asRecord(payload);
  } catch {
    return null;
  }
}

async function loadStoredActivityObject(objectId: string): Promise<{
  kind: "post" | "comment";
  objectId: string;
  inReplyToObjectId: string | null;
} | null> {
  // Local objects are resolved from the shared DB first so reply chains remain
  // valid even when no HTTP route or in-memory state is available yet.
  const databaseUrl = process.env.DATABASE_URL ?? "sqlite:///./bridge.db";
  const row = await loadPublishedActivityObjectByObjectIdForDatabaseUrl(
    databaseUrl,
    objectId,
  );
  if (row == null) {
    return null;
  }
  return {
    kind: row.kind,
    objectId: row.objectId,
    inReplyToObjectId: row.inReplyToObjectId,
  };
}

function isCommentLikeReplyTarget(
  replyTarget: string,
  parentRecord: Record<string, unknown>,
): boolean {
  return isLemmyPath(replyTarget, "comment") || parentRecord.type === "Note";
}

function parseReplyTargetPostNumericId(
  postApId: string,
  postSource: "local" | "remote",
): number {
  if (postSource === "local") {
    return 0;
  }
  const parsed = tryParseLemmyNumericId(postApId, "post");
  return parsed ?? 0;
}

function tryParseLemmyNumericId(
  value: string,
  kind: "post" | "comment",
): number | null {
  const pattern = new RegExp(`/${kind}/(\\d+)(?:$|/)`);
  const match = pattern.exec(new URL(value).pathname);
  if (!match) {
    return null;
  }
  return Number.parseInt(match[1], 10);
}

function parseRequiredLemmyNumericId(value: string, kind: "post" | "comment"): number {
  const parsed = tryParseLemmyNumericId(value, kind);
  if (parsed == null) {
    throw new Error(`Could not parse Lemmy ${kind} numeric id from ${value}`);
  }
  return parsed;
}

function isLemmyPath(value: string, kind: "post" | "comment"): boolean {
  return new RegExp(`/${kind}/\\d+(?:$|/)`).test(new URL(value).pathname);
}

function isCommunityActorId(value: string): boolean {
  return /\/c\/[^/]+(?:$|\/)/.test(new URL(value).pathname);
}

function toIsoString(value: { toString(): string } | null | undefined): string {
  return value?.toString() ?? new Date().toISOString();
}

function lastPathSegment(value: string): string {
  const pathname = new URL(value).pathname;
  const parts = pathname.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? value;
}

function inferKindFromApId(apId: string): "post" | "comment" | null {
  // Lemmy AP IDs use /post/<n> and /comment/<n> path conventions.
  try {
    const { pathname } = new URL(apId);
    if (/\/post\/\d+/.test(pathname)) return "post";
    if (/\/comment\/\d+/.test(pathname)) return "comment";
  } catch {
    // Non-URL ap_id — cannot infer kind.
  }
  return null;
}

function parseLemmyNumericIdOrZero(apId: string, kind: "post" | "comment"): number {
  // Returns 0 when the id cannot be parsed — acceptable for Delete events
  // because no delete handler reads the lemmy_id field.
  return tryParseLemmyNumericId(apId, kind) ?? 0;
}

function resolveCommunityActorIdForDelete(
  activity: Record<string, unknown>,
): string | null {
  // audience is present on the Delete record itself in Lemmy 0.19 (confirmed from real logs).
  // Fall back to the first /c/ community found in cc or to.
  const audience = asString(activity.audience);
  if (audience && isCommunityActorId(audience)) {
    return audience;
  }

  const candidates = [
    ...normalizeStringArray(activity.cc),
    ...normalizeStringArray(activity.to),
  ];
  return candidates.find((c) => isCommunityActorId(c)) ?? null;
}
