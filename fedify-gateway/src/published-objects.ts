import type { PublishedActivityObjectRow } from "./db.js";

export function buildPublishedActivityObjectJson(
  record: PublishedActivityObjectRow,
): Record<string, unknown> {
  // Stored local objects must stay reconstructible after a restart, so the
  // route builds a deterministic AP payload directly from the durable DB row.
  const base = {
    "@context": "https://www.w3.org/ns/activitystreams",
    id: record.objectId,
    attributedTo: record.actorUrl,
    audience: record.communityActorUrl,
    to: ["as:Public", record.communityActorUrl],
    cc: [record.actorUrl],
    source: {
      content: record.bodyMarkdown,
      mediaType: "text/markdown",
    },
    content: record.bodyMarkdown,
    published: record.publishedAt,
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
