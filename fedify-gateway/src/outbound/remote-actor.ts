/**
 * Remote community Actor resolution through Fedify's hardened document loader.
 *
 * Caller-provided Actor and inbox URLs must not cross this boundary via a
 * general-purpose fetch. Only Fedify's origin-checked lookup may supply the
 * Actor's identity and inbox for outbound ActivityPub delivery.
 */
import { admitRemoteDeliveryEndpoint } from "../remote-delivery-endpoints.js";

/** The narrow part of Fedify Context needed to dereference an Actor. */
export interface RemoteActorLookupContext {
  lookupObject(
    identifier: string,
    options?: { crossOrigin?: "throw" },
  ): Promise<unknown>;
}

/** Resolve one remote community actor and check its canonical ID and inbox. */
export async function fetchRemoteCommunity(
  ctx: RemoteActorLookupContext,
  communityActorUrl: string,
): Promise<{ communityId: string; inboxUrl: string }> {
  // URL admission is an early filter. Fedify's document loader must also
  // inspect resolved socket addresses and redirects during the actual GET.
  const requested = admitRemoteDeliveryEndpoint(communityActorUrl);
  const actor = await ctx.lookupObject(requested, { crossOrigin: "throw" });
  if (actor == null || typeof actor !== "object") {
    throw new Error("remote community actor could not be verified");
  }
  const identity = (actor as { id?: unknown }).id;
  const inbox = (actor as { inboxId?: unknown }).inboxId;
  if (!(identity instanceof URL) || identity.href !== requested) {
    throw new Error("remote community actor identity mismatch");
  }
  if (!(inbox instanceof URL)) {
    throw new Error("remote community actor does not have an inbox");
  }
  const admittedInbox = admitRemoteDeliveryEndpoint(inbox.href);
  return { communityId: identity.href, inboxUrl: admittedInbox };
}
