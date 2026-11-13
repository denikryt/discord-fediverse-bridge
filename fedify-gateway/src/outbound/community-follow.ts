/**
 * Outbound bridge actor Follow and Undo(Follow) publication.
 *
 * This module owns remote community actor lookup for follow operations, Fedify
 * Follow delivery, and the signed JSON Lemmy-compatible Undo(Follow) path.
 */

import { Follow, Undo } from "@fedify/vocab";
import { createHash } from "node:crypto";
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

/** Prepare an immutable Follow identity and verified target without sending. */
export async function prepareCommunityFollow(
  config: GatewayConfig,
  communityActorUrl: string,
): Promise<FollowCommunityResult> {
  // The Python writer must commit this complete tuple and a channel binding
  // before a sender is allowed to contact the remote inbox.
  const { communityId, inboxUrl } = await fetchRemoteCommunity(communityActorUrl);
  return {
    communityActorUrl: communityId,
    communityInboxUrl: inboxUrl,
    followActivityId: buildServiceFollowActivityId(
      config.fedifyOrigin,
      config.actorIdentifier,
      `${Date.now()}-${Math.random().toString(36).slice(2)}`,
    ).href,
  };
}

/** Send exactly the Follow reserved by the Python persistence boundary. */
export async function sendPreparedCommunityFollow(
  federation: Federation<GatewayConfig>,
  config: GatewayConfig,
  prepared: FollowCommunityResult,
): Promise<void> {
  // Never synthesize a replacement ID or re-fetch a possibly changed target:
  // both the original actor and inbox are part of the durable reservation.
  const actor = new URL(prepared.communityActorUrl);
  const inbox = new URL(prepared.communityInboxUrl);
  const followId = new URL(prepared.followActivityId);
  if (actor.protocol !== "https:" || inbox.protocol !== "https:") {
    throw new Error("prepared Follow recipient must use HTTPS");
  }
  const expectedPrefix = buildServiceFollowActivityId(
    config.fedifyOrigin, config.actorIdentifier, "placeholder",
  ).href.replace(/placeholder$/, "");
  if (!followId.href.startsWith(expectedPrefix) || followId.origin !== new URL(config.fedifyOrigin).origin) {
    throw new Error("prepared Follow ID does not belong to this bridge service");
  }
  const ctx = federation.createContext(new URL(config.fedifyOrigin), config);
  const actorTail = `services/${config.actorIdentifier}`;
  const follow = new Follow({
    id: followId,
    actor: ctx.getActorUri(actorTail),
    object: actor,
  });
  // This slash-containing tail is a dispatcher identifier, not a WebFinger
  // username. Fedify would call mapHandle("services/bridge") for username,
  // which cannot resolve the service key (the public handle is "bridge").
  await ctx.sendActivity(
    { identifier: actorTail },
    { id: actor, inboxId: inbox },
    follow,
  );
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
  // The service sender is addressed by the typed actor identifier, not by
  // its public WebFinger username ("bridge").
  await ctx.sendActivity(
    { identifier: serviceActorTail },
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
  originalInboxUrl?: string,
): Promise<void> {
  const ctx = federation.createContext(new URL(config.fedifyOrigin), config);
  const serviceActorTail = `services/${config.actorIdentifier}`;
  const actorUri = ctx.getActorUri(serviceActorTail);

  // The Undo must go to the original, verified Group recipient. Fetching a
  // changed actor document after cancellation could redirect this generation.
  const { communityId, inboxUrl } = originalInboxUrl == null
    ? await fetchRemoteCommunity(communityActorUrl)
    : { communityId: communityActorUrl, inboxUrl: originalInboxUrl };
  if (new URL(communityId).protocol !== "https:" || new URL(inboxUrl).protocol !== "https:") {
    throw new Error("saved Undo recipient must use HTTPS");
  }

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
      createHash("sha256").update(followActivityId, "utf8").digest("hex"),
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
