import { randomUUID } from "node:crypto";

import { Create, Link } from "@fedify/vocab";
import { Note } from "@fedify/vocab";
import { Page } from "@fedify/vocab";
import { type Object as ActivityObject } from "@fedify/vocab";

import {
  loadMessageMappingByObjectIdForDatabaseUrl,
  loadPublishedActivityObjectByObjectIdForDatabaseUrl,
} from "./db.js";
import type { BridgeEvent } from "./types.js";

export interface NormalizeOptions {
  /** Shared bridge database URL resolved by the gateway configuration. */
  databaseUrl?: string;
}

export async function normalizeCreateActivity(
  activity: Create,
  options: NormalizeOptions = {},
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
    return await normalizeCommentActivity(activity, object, options);
  }

  return null;
}

export async function normalizeCreateActivityFromJson(
  activity: unknown,
  options: NormalizeOptions = {},
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
    return await normalizeCommentActivityFromJson(activity, object, options);
  }

  return null;
}

export async function normalizeUpdateActivityFromJson(
  activity: unknown,
  options: NormalizeOptions = {},
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
    const event = await normalizeCommentActivityFromJson(activity, object, options);
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
  const communityActorUrl =
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
    community_actor_id: communityActorUrl,
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
  options: NormalizeOptions,
): Promise<BridgeEvent> {
  const apId = requireUrl(object.id, "comment object id");
  const replyTargetId = object.replyTargetId?.href;
  if (!replyTargetId) {
    throw new Error(`Comment ${apId} is missing inReplyTo/replyTarget`);
  }

  const replyContext = await resolveReplyChainContext(replyTargetId, options);
  if (!replyContext.postApId) {
    throw new Error(`Could not resolve post AP ID for comment ${apId}`);
  }

  const url = resolveObjectUrl(object) ?? apId;
  const publishedAt = toIsoString(object.published) ?? toIsoString(activity.published);

  return {
    actor_id: activity.actorId?.href ?? "",
    community_actor_id: await resolveCommentCommunityActorId(object, replyTargetId, options),
    delivery_id: activity.id?.href ?? randomUUID(),
    event_type: "comment.created",
    occurred_at: publishedAt,
    object: {
      ap_id: apId,
      author_name: await resolveAuthorName(activity),
      body_markdown: resolveMarkdownBody(object),
      kind: "comment",
      lemmy_id: parseLemmyNumericIdOrZero(apId, "comment"),
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
  const community = tryResolveCommunityActorId(object);
  if (!community) {
    throw new Error("Could not resolve community actor id from ActivityPub object");
  }
  return community;
}

function tryResolveCommunityActorId(object: ActivityObject): string | null {
  const candidates = [
    object.audienceId?.href,
    ...object.toIds.map((id) => id.href),
    ...object.ccIds.map((id) => id.href),
  ].filter((value): value is string => Boolean(value));

  return candidates.find((candidate) => isCommunityActorId(candidate)) ?? null;
}

async function resolveCommentCommunityActorId(
  object: Note,
  replyTargetId: string,
  options: NormalizeOptions,
): Promise<string> {
  // Lemmy-style comments address the community directly. Keep that path first
  // so existing community routing remains unchanged for normal Lemmy traffic.
  const addressedCommunity = tryResolveCommunityActorId(object);
  if (addressedCommunity) {
    logCommunityResolution("addressing", addressedCommunity, null);
    return addressedCommunity;
  }

  // Mastodon-shaped and other direct Note replies may address only the replied
  // actor. In that protocol shape the local parent mapping is the routing
  // authority because it carries the Discord placement and community actor.
  const parentMapping = await loadLocalParentMessageMapping(replyTargetId, options);
  if (parentMapping == null) {
    throw new Error(
      `Could not resolve community actor id for comment reply parent ${replyTargetId}`,
    );
  }
  logCommunityResolution(
    "local-parent",
    parentMapping.communityActorUrl,
    parentMapping.objectId,
  );
  return parentMapping.communityActorUrl;
}

function resolveMarkdownBody(object: ActivityObject): string | null {
  const sourceContent = object.source?.content;
  if (typeof sourceContent === "string" && sourceContent.length > 0) {
    return sourceContent;
  }
  if (typeof object.content === "string" && object.content.length > 0) {
    // Mastodon and similar servers send Note.content as rendered HTML. The
    // Python/Discord side expects this field to be readable message text, not
    // raw HTML that Discord will preview as broken links.
    return htmlContentToDiscordText(object.content);
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
  options: NormalizeOptions,
): Promise<BridgeEvent> {
  const apId = requireString(object.id, "comment object id");
  const replyTarget = asString(object.inReplyTo) ?? asString(object.replyTarget);
  if (!replyTarget) {
    throw new Error(`Comment ${apId} is missing inReplyTo`);
  }

  const replyContext = await resolveReplyChainContext(replyTarget, options);
  if (!replyContext.postApId) {
    throw new Error(`Could not resolve post AP ID for comment ${apId}`);
  }

  const publishedAt = asString(object.published) ?? asString(activity.published) ?? new Date().toISOString();

  return {
    actor_id: asString(activity.actor) ?? "",
    community_actor_id: await resolveCommentCommunityActorIdFromJson(object, replyTarget, options),
    delivery_id: asString(activity.id) ?? randomUUID(),
    event_type: "comment.created",
    occurred_at: publishedAt,
    object: {
      ap_id: apId,
      author_name: resolveAuthorNameFromJson(activity, object),
      body_markdown: resolveMarkdownBodyFromJson(object),
      kind: "comment",
      lemmy_id: parseLemmyNumericIdOrZero(apId, "comment"),
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
  const community = tryResolveCommunityActorIdFromJson(object);
  if (!community) {
    throw new Error("Could not resolve community actor id from raw ActivityPub object");
  }
  return community;
}

function tryResolveCommunityActorIdFromJson(
  object: Record<string, unknown>,
): string | null {
  const candidates = [
    ...normalizeStringArray(object.audience),
    ...normalizeStringArray(object.to),
    ...normalizeStringArray(object.cc),
  ];

  return candidates.find((candidate) => isCommunityActorId(candidate)) ?? null;
}

async function resolveCommentCommunityActorIdFromJson(
  object: Record<string, unknown>,
  replyTarget: string,
  options: NormalizeOptions,
): Promise<string> {
  // Raw Announce(Create(Note)) payloads from Lemmy continue to use explicit
  // community addressing; only missing addressing falls back to a local parent.
  const addressedCommunity = tryResolveCommunityActorIdFromJson(object);
  if (addressedCommunity) {
    logCommunityResolution("addressing", addressedCommunity, null);
    return addressedCommunity;
  }

  const parentMapping = await loadLocalParentMessageMapping(replyTarget, options);
  if (parentMapping == null) {
    throw new Error(
      `Could not resolve community actor id for raw comment reply parent ${replyTarget}`,
    );
  }
  logCommunityResolution(
    "local-parent",
    parentMapping.communityActorUrl,
    parentMapping.objectId,
  );
  return parentMapping.communityActorUrl;
}

function resolveMarkdownBodyFromJson(object: Record<string, unknown>): string | null {
  const source = asRecord(object.source);
  const sourceContent = asString(source?.content);
  if (sourceContent) {
    return sourceContent;
  }
  const content = asString(object.content);
  return content ? htmlContentToDiscordText(content) : null;
}


function htmlContentToDiscordText(content: string): string {
  // ActivityPub content is often HTML. Convert only the rendered-content
  // fallback path; source.content with text/markdown is preserved above.
  let text = content
    .replace(/<br\s*\/?\s*>/gi, "\n")
    .replace(/<\/p\s*>/gi, "\n\n")
    .replace(/<\/div\s*>/gi, "\n")
    .replace(/<\/li\s*>/gi, "\n")
    .replace(/<[^>]+>/g, "");

  text = decodeBasicHtmlEntities(text)
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();

  // Mastodon replies commonly begin with a mention of the replied local actor.
  // Discord already places the message in the reply thread, so keep the actual
  // body and drop that routing mention from the rendered bridge text.
  return text.replace(/^@\S+\s*(?:\n+|\s+)/, "").trim();
}

function decodeBasicHtmlEntities(value: string): string {
  // Keep this dependency-free and conservative; these are the entities that
  // normally appear in sanitized ActivityPub HTML bodies. Numeric entities are
  // decoded so non-ASCII replies remain readable in Discord.
  return value
    .replace(/&nbsp;/g, " ")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, "&")
    .replace(/&#(\d+);/g, (_match, codepoint: string) => {
      const parsed = Number.parseInt(codepoint, 10);
      return Number.isFinite(parsed) ? String.fromCodePoint(parsed) : _match;
    })
    .replace(/&#x([0-9a-f]+);/gi, (_match, codepoint: string) => {
      const parsed = Number.parseInt(codepoint, 16);
      return Number.isFinite(parsed) ? String.fromCodePoint(parsed) : _match;
    });
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
  options: NormalizeOptions,
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

  const storedObject = await loadStoredActivityObject(replyTarget, options);
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
      options,
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
  const nestedContext = await resolveReplyChainContext(nextReplyTarget, options, visited);
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

async function loadLocalParentMessageMapping(
  objectId: string,
  options: NormalizeOptions,
): Promise<{
  objectId: string;
  communityActorUrl: string;
} | null> {
  // Use message_mappings, not published_activity_objects, because only the
  // mapping table proves that the parent has Discord placement state.
  const row = await loadMessageMappingByObjectIdForDatabaseUrl(
    resolveDatabaseUrl(options),
    objectId,
  );
  if (row == null) {
    return null;
  }
  return {
    objectId: row.objectId,
    communityActorUrl: row.communityActorUrl,
  };
}

function resolveDatabaseUrl(options: NormalizeOptions): string {
  // Runtime callers pass the gateway-resolved database URL so normalization
  // reads the same shared bridge DB as actor routes and published-object routes.
  if (options.databaseUrl) {
    return options.databaseUrl;
  }
  // Tests and standalone verify scripts may still provide DATABASE_URL directly,
  // but production code must not depend on cwd-relative fallbacks here.
  if (process.env.DATABASE_URL) {
    return process.env.DATABASE_URL;
  }
  return "sqlite:///../bridge.db";
}

function logCommunityResolution(
  source: "addressing" | "local-parent",
  communityActorUrl: string,
  parentObjectId: string | null,
): void {
  // LOG_LEVEL=debug is the single project-wide switch for gateway diagnostics.
  // Logging the source makes routing decisions auditable without changing the
  // bridge event schema.
  if (process.env.LOG_LEVEL !== "debug") {
    return;
  }
  console.log("[Fedify][debug] Resolved comment community", {
    source,
    communityActorUrl,
    parentObjectId,
  });
}

async function loadStoredActivityObject(
  objectId: string,
  options: NormalizeOptions,
): Promise<{
  kind: "post" | "comment";
  objectId: string;
  inReplyToObjectId: string | null;
} | null> {
  // Local objects are resolved from the shared DB first so reply chains remain
  // valid even when no HTTP route or in-memory state is available yet.
  const row = await loadPublishedActivityObjectByObjectIdForDatabaseUrl(
    resolveDatabaseUrl(options),
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
  const pathname = new URL(value).pathname;
  return (
    /\/c\/[^/]+(?:$|\/)/.test(pathname) ||
    /\/communities\/[^/]+(?:$|\/)/.test(pathname)
  );
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
  // Fall back to the first community actor id found in cc or to.
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
