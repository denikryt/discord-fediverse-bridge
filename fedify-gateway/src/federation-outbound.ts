import { Follow } from "@fedify/vocab";
import type { Federation } from "@fedify/fedify";
import type { GatewayConfig } from "./config.js";
import { FileKeyStore } from "./key-store.js";

export async function followCommunity(
  federation: Federation<GatewayConfig>,
  config: GatewayConfig,
  communityActorUrl: string,
): Promise<void> {
  const ctx = federation.createContext(new URL(config.fedifyOrigin), config);
  const actorUri = ctx.getActorUri(config.actorIdentifier);

  // Get community's inbox by fetching its actor object
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

  // Create Follow activity with required id field
  const follow = new Follow({
    id: new URL(
      `${config.fedifyOrigin}activities/follow/${Date.now()}/${Math.random().toString(36).slice(2)}`,
    ),
    actor: actorUri,
    object: new URL(communityId),
  });

  console.log("[Follow] Sending Follow activity:", {
    actorUri: actorUri.toString(),
    communityId,
    inboxUrl,
    followId: follow.id?.toString(),
  });

  // Send signed request to inbox
  await ctx.sendActivity(
    { username: config.actorIdentifier },
    { id: new URL(communityId), inboxId: new URL(inboxUrl) },
    follow,
  );

  console.log("[Follow] Successfully sent Follow activity");
}
