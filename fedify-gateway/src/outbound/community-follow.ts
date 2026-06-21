/**
 * Outbound bridge actor Follow and Undo(Follow) publication.
 *
 * This module owns remote community actor lookup for follow operations, Fedify
 * Follow delivery, and the signed JSON Lemmy-compatible Undo(Follow) path.
 */

import { Follow, Undo } from "@fedify/vocab";
import type { Federation } from "@fedify/fedify";

import type { GatewayConfig } from "../config.js";
import { appendDebugFileLog } from "../debug-file-log.js";
import { loadActorKeyPair } from "../actor-store.js";
import { buildServiceFollowActivityId, buildServiceUndoFollowActivityId } from "../activitypub-identity-urls.js";
import { renderPublicActivityJson } from "./activity-json.js";
import { fetchRemoteCommunity } from "./remote-actor.js";
import { sendSignedJsonActivity, shouldLogSignedJsonDelivery } from "./signed-json-delivery.js";

export interface FollowCommunityResult {
  communityActorUrl: string;
  communityInboxUrl: string;
  followActivityId: string;
}

/** Publish one bridge actor Follow activity to a remote community actor. */
export async function followCommunity(
  federation: Federation<GatewayConfig>,
  config: GatewayConfig,
  communityActorUrl: string,
): Promise<FollowCommunityResult> {
  // Outbound follow is kept explicit and separate from inbound delivery so the
  // gateway can be pointed at a known community actor before bot commands exist.
  const ctx = federation.createContext(new URL(config.fedifyOrigin), config);
  const serviceActorTail = `services/${config.actorIdentifier}`;
  const actorUri = ctx.getActorUri(serviceActorTail);

  // Fetch the remote actor first because Follow delivery needs both the actor
  // id and its concrete inbox endpoint.
  const { communityId, inboxUrl } = await fetchRemoteCommunity(communityActorUrl);

  // Build a unique Follow id locally so retries and remote logs can refer to a
  // concrete outbound activity URL.
  const follow = new Follow({
    id: buildServiceFollowActivityId(
      config.fedifyOrigin,
      config.actorIdentifier,
      `${Date.now()}-${Math.random().toString(36).slice(2)}`,
    ),
    actor: actorUri,
    object: new URL(communityId),
  });

  const followJson = await follow.toJsonLd();
  console.log("[Follow] Sending Follow activity:", {
    actorUri: actorUri.toString(),
    communityId,
    inboxUrl,
    followId: follow.id?.toString(),
  });
  appendDebugFileLog("follow.outbound", {
    actorUri: actorUri.toString(),
    communityActorUrl,
    communityId,
    inboxUrl,
    followActivityId: follow.id?.toString() ?? null,
    activity: followJson as Record<string, unknown>,
  });

  // Fedify signs and delivers the activity; this helper only prepares the
  // target identity and object URLs.
  await ctx.sendActivity(
    { username: serviceActorTail },
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

/** Publish one Lemmy-compatible signed Undo(Follow) for an earlier bridge Follow. */
export async function unfollowCommunity(
  federation: Federation<GatewayConfig>,
  config: GatewayConfig,
  communityActorUrl: string,
  followActivityId: string,
): Promise<void> {
  const ctx = federation.createContext(new URL(config.fedifyOrigin), config);
  const serviceActorTail = `services/${config.actorIdentifier}`;
  const actorUri = ctx.getActorUri(serviceActorTail);

  const { communityId, inboxUrl } = await fetchRemoteCommunity(communityActorUrl);

  const undo = buildLemmyCompatibleUnfollowActivity(
    config,
    actorUri,
    communityId,
    followActivityId,
  );

  const undoJson = await renderPublicActivityJson(undo);
  console.log("[Unfollow] Sending Undo(Follow) activity:", {
    actorUri: actorUri.toString(),
    communityId,
    inboxUrl,
    followActivityId,
    undoId: undo.id?.toString(),
    deliveryBackend: "signed-json",
  });
  appendDebugFileLog("unfollow.outbound", {
    actorUri: actorUri.toString(),
    communityActorUrl,
    communityId,
    inboxUrl,
    followActivityId,
    undoActivityId: undo.id?.toString() ?? null,
    deliveryBackend: "signed-json",
    activity: undoJson,
  });

  const keyPair = await loadActorKeyPair(config, serviceActorTail);
  if (keyPair == null) {
    throw new Error("Bridge actor signing key not found");
  }
  await sendSignedJsonActivity(
    "Unfollow",
    inboxUrl,
    { keyId: new URL("#main-key", actorUri), privateKey: keyPair.privateKey },
    undoJson,
    { debugDelivery: shouldLogSignedJsonDelivery(config) },
  );

  console.log("[Unfollow] Successfully sent Undo(Follow) activity", {
    deliveryBackend: "signed-json",
  });
}

/** Build the embedded Follow shape Lemmy expects inside Undo(Follow). */
export function buildLemmyCompatibleUnfollowActivity(
  config: GatewayConfig,
  actorUri: URL,
  communityId: string,
  followActivityId: string,
): Undo {
  // Lemmy 0.19.x models Undo(Follow) with an embedded Follow object rather
  // than an IRI. Including the community in both outer and inner `to` mirrors
  // Lemmy's own fixture shape and lets Lemmy resolve the target community
  // before it deletes the corresponding remote-subscriber row.
  const communityActorUrl = new URL(communityId);
  return new Undo({
    id: buildServiceUndoFollowActivityId(
      config.fedifyOrigin,
      config.actorIdentifier,
      `${Date.now()}-${Math.random().toString(36).slice(2)}`,
    ),
    actor: actorUri,
    object: new Follow({
      id: new URL(followActivityId),
      actor: actorUri,
      object: communityActorUrl,
      tos: [communityActorUrl],
    }),
    tos: [communityActorUrl],
  });
}
