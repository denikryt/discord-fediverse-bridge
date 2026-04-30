import { randomUUID } from "node:crypto";

import { Create } from "@fedify/vocab";
import { Note } from "@fedify/vocab";
import { Page } from "@fedify/vocab";
import { type Object as ActivityObject } from "@fedify/vocab";

import type { BridgeEvent } from "./types.js";

export async function normalizeCreateActivity(
  activity: Create,
): Promise<BridgeEvent | null> {
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

export function normalizeCreateActivityFromJson(
  activity: unknown,
): BridgeEvent | null {
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
    return normalizeCommentActivityFromJson(activity, object);
  }

  return null;
}

async function normalizePostActivity(
  activity: Create,
  object: Page,
): Promise<BridgeEvent> {
  const apId = requireUrl(object.id, "post object id");
  const url = resolveObjectUrl(object) ?? apId;
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

  const parentApId = isLemmyPath(replyTargetId, "comment") ? replyTargetId : null;
  const postApId = await resolvePostApId(object);
  if (!postApId) {
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
      parent_ap_id: parentApId,
      post_ap_id: postApId,
      post_lemmy_id: parseRequiredLemmyNumericId(postApId, "post"),
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

async function resolvePostApId(note: Note): Promise<string | null> {
  const replyTargetId = note.replyTargetId?.href;
  if (!replyTargetId) {
    return null;
  }
  if (isLemmyPath(replyTargetId, "post")) {
    return replyTargetId;
  }

  const replyTarget = await note.getReplyTarget({
    suppressError: true,
    crossOrigin: "trust",
  });
  if (replyTarget == null) {
    return null;
  }

  if (replyTarget instanceof Note) {
    return await resolvePostApId(replyTarget);
  }
  const targetId = requireUrl(replyTarget.id, "reply target id");
  if (isLemmyPath(targetId, "post")) {
    return targetId;
  }
  return null;
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
      url: asString(object.url) ?? apId,
    },
  };
}

function normalizeCommentActivityFromJson(
  activity: Record<string, unknown>,
  object: Record<string, unknown>,
): BridgeEvent {
  const apId = requireString(object.id, "comment object id");
  const replyTarget = asString(object.inReplyTo) ?? asString(object.replyTarget);
  if (!replyTarget) {
    throw new Error(`Comment ${apId} is missing inReplyTo`);
  }

  const postApId = resolvePostApIdFromJson(replyTarget);
  if (!postApId) {
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
      parent_ap_id: isLemmyPath(replyTarget, "comment") ? replyTarget : null,
      post_ap_id: postApId,
      post_lemmy_id: parseRequiredLemmyNumericId(postApId, "post"),
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

function resolvePostApIdFromJson(replyTarget: string): string | null {
  if (isLemmyPath(replyTarget, "post")) {
    return replyTarget;
  }
  return null;
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

function parseRequiredLemmyNumericId(value: string, kind: "post" | "comment"): number {
  const pattern = new RegExp(`/${kind}/(\\d+)(?:$|/)`);
  const match = pattern.exec(new URL(value).pathname);
  if (!match) {
    throw new Error(`Could not parse Lemmy ${kind} numeric id from ${value}`);
  }
  return Number.parseInt(match[1], 10);
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
