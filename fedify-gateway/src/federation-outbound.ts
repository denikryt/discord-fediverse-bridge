import { Follow } from "@fedify/vocab";
import type { Federation } from "@fedify/fedify";
import type { GatewayConfig } from "./config.js";

export interface FollowCommunityResult {
  communityActorUrl: string;
  communityInboxUrl: string;
  followActivityId: string;
}

export async function followCommunity(
  federation: Federation<GatewayConfig>,
  config: GatewayConfig,
  communityActorUrl: string,
): Promise<FollowCommunityResult> {
  // Outbound follow is kept explicit and separate from inbound delivery so the
  // gateway can be pointed at a known community actor before bot commands exist.
  const ctx = federation.createContext(new URL(config.fedifyOrigin), config);
  const actorUri = ctx.getActorUri(config.actorIdentifier);

  // Fetch the remote actor first because Follow delivery needs both the actor
  // id and its concrete inbox endpoint.
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

  // Build a unique Follow id locally so retries and remote logs can refer to a
  // concrete outbound activity URL.
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

  // Fedify signs and delivers the activity; this helper only prepares the
  // target identity and object URLs.
  await ctx.sendActivity(
    { username: config.actorIdentifier },
    { id: new URL(communityId), inboxId: new URL(inboxUrl) },
    follow,
  );

  console.log("[Follow] Successfully sent Follow activity");
  return {
    communityActorUrl: communityId,
    communityInboxUrl: inboxUrl,
    followActivityId: follow.id?.href ?? follow.id?.toString() ?? "",
  };
}
