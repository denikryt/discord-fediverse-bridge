/**
 * Accept(Follow) delivery for User Actor and local-community followers.
 *
 * Python performs inbound admission and persistence before calling these
 * functions. The gateway loads the minimal signing key at delivery time and
 * sends the Accept from the actor that owns the follower relationship.
 */

import { createHash } from "node:crypto";

import { Accept, Follow } from "@fedify/vocab";
import type { Federation } from "@fedify/fedify";

import type { GatewayConfig } from "../config.js";
import { loadActorKeyPair, loadUserActorIdentity } from "../actor-store.js";
import { loadLocalCommunitySigningKey } from "../local-community-keys.js";
import type { AcceptLocalCommunityFollowRequest, AcceptUserActorFollowRequest } from "../types.js";
import { buildGatewayUrl } from "./gateway-url.js";

/** Send a deterministic Accept(Follow) as one registered User Actor. */
export async function acceptUserActorFollow(
  federation: Federation<GatewayConfig>,
  config: GatewayConfig,
  request: AcceptUserActorFollowRequest,
): Promise<void> {
  // Sign and deliver one deterministic Accept as the registered User Actor.
  const identity = await loadUserActorIdentity(config, request.actorUsername);
  if (identity == null || identity.actorId.href !== request.actorUrl) {
    throw new Error("actorUsername and actorUrl must identify the same registered user");
  }
  const keyPair = await loadActorKeyPair(config, request.actorUsername);
  if (keyPair == null) throw new Error("User Actor signing key not found");
  const hash = createHash("sha256").update(request.followActivityId).digest("hex");
  const accept = new Accept({
    id: buildGatewayUrl(config, `/actors/${request.actorUsername}/activities/accept/${hash}`),
    actor: new URL(request.actorUrl),
    to: new URL(request.remoteActorId),
    object: new Follow({
      id: new URL(request.followActivityId),
      actor: new URL(request.remoteActorId),
      object: new URL(request.actorUrl),
    }),
  });
  const ctx = federation.createContext(new URL(config.fedifyOrigin), config);
  await ctx.sendActivity(
    [{ keyId: new URL(`${request.actorUrl}#main-key`), privateKey: keyPair.privateKey }],
    { id: new URL(request.remoteActorId), inboxId: new URL(request.remoteInboxUrl) },
    accept,
  );
}

/** Send a local-community Accept(Follow) after Python persists the follower. */
export async function acceptLocalCommunityFollow(
  federation: Federation<GatewayConfig>,
  config: GatewayConfig,
  request: AcceptLocalCommunityFollowRequest,
): Promise<void> {
  // Python persists local-community followers first, then asks the gateway to
  // sign and deliver the Accept from the community actor.
  const ctx = federation.createContext(new URL(config.fedifyOrigin), config);
  const communityActorUri = new URL(request.communityActorUrl);
  const remoteActorUri = new URL(request.remoteActorId);
  const signingKey = await loadLocalCommunitySigningKey(config, request.communitySlug);
  if (signingKey == null) {
    throw new Error("Local community signing key not found");
  }
  if (signingKey.actorId.href !== communityActorUri.href) {
    throw new Error("communityActorUrl must match the canonical local community actor URL");
  }
  const sender = [{ keyId: signingKey.keyId, privateKey: signingKey.privateKey }];
  const acceptId = buildGatewayUrl(
    config,
    `/communities/${request.communitySlug}/activities/accept/${Date.now()}-${Math.random().toString(36).slice(2)}`,
  );

  const accept = new Accept({
    id: acceptId,
    actor: communityActorUri,
    to: remoteActorUri,
    object: new Follow({
      id: new URL(request.followActivityId),
      actor: remoteActorUri,
      object: communityActorUri,
    }),
  });

  console.log("[LocalCommunityFollow] Sending Accept(Follow):", {
    communitySlug: request.communitySlug,
    communityActorUrl: request.communityActorUrl,
    remoteActorId: request.remoteActorId,
    remoteInboxUrl: request.remoteInboxUrl,
    followActivityId: request.followActivityId,
    acceptId: acceptId.href,
    signingKeyId: signingKey.keyId.href,
  });
  try {
    await ctx.sendActivity(
      sender,
      { id: remoteActorUri, inboxId: new URL(request.remoteInboxUrl) },
      accept,
    );
    console.log("[LocalCommunityFollow] Accept(Follow) delivered:", {
      communitySlug: request.communitySlug,
      remoteActorId: request.remoteActorId,
      followActivityId: request.followActivityId,
      acceptId: acceptId.href,
      signingKeyId: signingKey.keyId.href,
    });
  } catch (error) {
    console.error("[LocalCommunityFollow] Accept(Follow) failed:", {
      communitySlug: request.communitySlug,
      remoteActorId: request.remoteActorId,
      remoteInboxUrl: request.remoteInboxUrl,
      followActivityId: request.followActivityId,
      signingKeyId: signingKey.keyId.href,
      error: error instanceof Error ? error.message : String(error),
    });
    throw error;
  }
}
