/**
 * Shared route response helpers for gateway HTTP handlers.
 *
 * The helpers in this module are deliberately tiny so route extraction cannot
 * normalize status codes or error bodies as a side effect of moving handlers.
 */

/** Return one ActivityPub JSON document with the gateway's existing content type. */
export function activityJsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: {
      "Content-Type": "application/activity+json",
    },
  });
}
