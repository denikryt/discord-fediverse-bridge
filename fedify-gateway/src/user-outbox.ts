/** Render the canonical paginated User Actor outbox from Python-owned state. */
import type { UserOutboxPageRow } from "./python-bridge-client.js";
import { buildPublishedCreateActivityJson } from "./published-objects.js";


/** Validate the Python-owned canonical outbox URL before exposing it publicly. */
export function validateCanonicalUserOutboxUrl(
  outboxUrl: string,
  fedifyOrigin: string,
  username: string,
): string {
  let candidate: URL;
  let origin: URL;
  try {
    candidate = new URL(outboxUrl);
    origin = new URL(fedifyOrigin);
  } catch {
    throw new Error("invalid canonical user outbox URL");
  }
  const expectedPath = `/actors/users/${encodeURIComponent(username)}/outbox`;
  if (
    candidate.origin !== origin.origin ||
    candidate.username !== "" ||
    candidate.password !== "" ||
    candidate.search !== "" ||
    candidate.hash !== "" ||
    candidate.pathname !== expectedPath
  ) {
    throw new Error("inconsistent canonical user outbox URL");
  }
  return candidate.toString();
}

/** Build an OrderedCollection root or one numbered OrderedCollectionPage. */
export function buildUserOutboxJson(
  row: UserOutboxPageRow,
  requestedPage: number | null,
): Record<string, unknown> {
  const root = row.outboxUrl;
  if (requestedPage == null) {
    return {
      "@context": "https://www.w3.org/ns/activitystreams",
      id: root,
      type: "OrderedCollection",
      totalItems: row.totalItems,
      ...(row.totalItems > 0 ? { first: `${root}?page=1` } : {}),
    };
  }
  const page = row.page;
  const start = (page - 1) * row.pageSize;
  const hasNext = start + row.items.length < row.totalItems;
  return {
    "@context": "https://www.w3.org/ns/activitystreams",
    id: `${root}?page=${page}`,
    type: "OrderedCollectionPage",
    partOf: root,
    totalItems: row.totalItems,
    orderedItems: row.items.map(buildPublishedCreateActivityJson),
    ...(page > 1 ? { prev: `${root}?page=${page - 1}` } : {}),
    ...(hasNext ? { next: `${root}?page=${page + 1}` } : {}),
  };
}
