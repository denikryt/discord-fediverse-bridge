import type { PublishedActivityObjectRow } from "./python-bridge-client.js";

const ACTIVITYSTREAMS_PUBLIC = "https://www.w3.org/ns/activitystreams#Public";

/**
 * Render one durable local Page/Note object for ActivityPub dereferencing.
 *
 * Mastodon may fetch the embedded object id after accepting an Announce(Create),
 * so this renderer must match the public addressing and author identity used on
 * the outbound wire payload rather than exposing weaker DB-only defaults.
 */
export function buildPublishedActivityObjectJson(
  record: PublishedActivityObjectRow,
): Record<string, unknown> {
  const attributedTo = canonicalActorUrlForRecord(record);
  const published = normalizePublishedTimestamp(record.publishedAt);
  const htmlContent = markdownToHtml(record.bodyMarkdown);

  // Stored local objects must stay reconstructible after a restart, so the
  // route builds a deterministic AP payload directly from the durable DB row.
  const base = {
    "@context": "https://www.w3.org/ns/activitystreams",
    id: record.objectId,
    attributedTo,
    audience: record.communityActorUrl,
    to: [ACTIVITYSTREAMS_PUBLIC, record.communityActorUrl],
    cc: [attributedTo],
    source: {
      content: record.bodyMarkdown,
      mediaType: "text/markdown",
    },
    content: htmlContent,
    published,
    url: record.objectId,
  };

  if (record.kind === "post") {
    return {
      ...base,
      type: "Page",
      name: record.title ?? "Untitled Discord Post",
    };
  }

  return {
    ...base,
    type: "Note",
    ...(record.inReplyToObjectId
      ? { inReplyTo: record.inReplyToObjectId }
      : {}),
  };
}

/**
 * Render the durable embedded Create activity for ActivityPub dereferencing.
 *
 * Mastodon dereferences Announce.object.id after accepting local-community
 * Announce(Create(Page|Note)) deliveries, so the stored Create must be
 * fetchable from bridge-owned persistence instead of existing only inside the original POST body.
 */
export function buildPublishedCreateActivityJson(
  record: PublishedActivityObjectRow,
): Record<string, unknown> {
  const actor = canonicalActorUrlForRecord(record);
  return {
    "@context": "https://www.w3.org/ns/activitystreams",
    id: record.activityId,
    type: "Create",
    actor,
    to: [ACTIVITYSTREAMS_PUBLIC, record.communityActorUrl],
    cc: [actor],
    object: buildPublishedActivityObjectJson(record),
  };
}

function canonicalActorUrlForRecord(record: PublishedActivityObjectRow): string {
  // Published object rows may predate the /actors user migration and still store
  // /users/{username}. Use the durable username plus the stored origin to expose
  // the canonical registered-user actor identity during object dereference.
  const storedActorUrl = new URL(record.actorUrl);
  return new URL(`/actors/${record.actorUsername}`, storedActorUrl.origin).href;
}

function normalizePublishedTimestamp(value: string): string {
  // Persisted rows can contain either RFC3339 strings or Python datetime strings.
  // Mastodon expects ActivityStreams-compatible timestamps, so convert common
  // UTC-like storage forms to an explicit Z-suffixed ISO string.
  const normalized = value.includes("T") ? value : value.replace(" ", "T");
  if (/[zZ]$|[+-]\d\d:\d\d$/.test(normalized)) {
    return normalized.replace(/z$/, "Z");
  }
  return `${normalized}Z`;
}

function markdownToHtml(markdown: string): string {
  // Keep this formatter intentionally aligned with federation-outbound.ts until
  // the gateway grows a dedicated shared markdown module.
  return markdown
    .split(/\n\n+/)
    .map((paragraph) => `<p>${paragraph.trim().replace(/\n/g, "<br>")}</p>`)
    .join("\n");
}
