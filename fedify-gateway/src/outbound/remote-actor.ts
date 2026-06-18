/**
 * Remote community actor lookup for outbound gateway use cases.
 *
 * The helper preserves the existing fetch headers and validation errors while
 * avoiding any signing, fanout, or subscription-policy responsibility.
 */

/** Fetch one remote community actor and return its canonical id and inbox. */
export async function fetchRemoteCommunity(
  communityActorUrl: string,
): Promise<{ communityId: string; inboxUrl: string }> {
  // Both follow and publish need the remote actor's canonical id and inbox, so
  // the fetch/validation logic is shared here instead of being repeated.
  const communityResponse = await fetch(communityActorUrl, {
    headers: {
      Accept: "application/activity+json",
    },
  });
  if (!communityResponse.ok) {
    throw new Error(
      `Failed to fetch community actor: ${communityResponse.status}`,
    );
  }
  const communityActor = await communityResponse.json();
  const inboxUrl = communityActor.inbox;
  const communityId = communityActor.id;

  if (!inboxUrl) {
    throw new Error("Community actor does not have an inbox");
  }
  if (!communityId) {
    throw new Error("Community actor does not have an id");
  }

  return { communityId, inboxUrl };
}
