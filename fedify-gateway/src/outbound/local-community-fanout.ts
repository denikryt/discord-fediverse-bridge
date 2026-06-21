/**
 * Local-community outbound fanout for Creates and canonical mutations.
 *
 * This module owns accepted remote-subscriber selection, local-community signing
 * key validation, Announce envelope construction, and the existing per-use-case
 * failure behavior. Durable retry and follower persistence remain Python-owned.
 */

import { Announce, Create, Delete, Update } from "@fedify/vocab";
import type { Federation } from "@fedify/fedify";

import type { GatewayConfig } from "../config.js";
import { loadLocalCommunitySigningKeyByActorUrl } from "../local-community-keys.js";
import { loadAcceptedRemoteSubscribersByActorUrl } from "../python-bridge-client.js";
import type { PublishLocalCommunityContentRequest, PublishLocalCommunityContentResult } from "../types.js";
import { buildGatewayUrl } from "./gateway-url.js";
import { renderPublicActivityJson } from "./activity-json.js";
import { buildPublishCreateActivity } from "./content-publish.js";
import { sendSignedJsonActivity, shouldLogSignedJsonDelivery } from "./signed-json-delivery.js";

/** Fan out one local-community Announce(Create) to accepted remote subscribers. */
export async function publishLocalCommunityContent(
  federation: Federation<GatewayConfig>,
  config: GatewayConfig,
  request: PublishLocalCommunityContentRequest,
): Promise<PublishLocalCommunityContentResult> {
  // Local-community fanout is performed by the bridge-owned community actor.
  // The embedded Create remains user-authored, while the outer Announce makes
  // delivery look like Lemmy Group fanout to Mastodon and other remote subscribers.
  const allRemoteSubscribers = await loadAcceptedRemoteSubscribersByActorUrl(
    config,
    request.communityActorUrl,
  );
  const explicitTargets = request.targetInboxUrls ?? null;
  const targetSet = explicitTargets == null ? null : new Set(explicitTargets);
  const remoteSubscribers = targetSet == null
    ? allRemoteSubscribers
    : allRemoteSubscribers.filter((subscriber) => targetSet.has(subscriber.remoteInboxUrl));

  const signingKey = await loadLocalCommunitySigningKeyByActorUrl(
    config,
    request.communityActorUrl,
  );
  if (signingKey == null) {
    throw new Error("Local community signing key not found");
  }
  if (signingKey.actorId.href !== request.communityActorUrl) {
    throw new Error("communityActorUrl must match the canonical local community actor URL");
  }
  const builtCreate = buildPublishCreateActivity(
    config,
    request,
    request.communityActorUrl,
  );
  const activity = buildLocalCommunityAnnounceActivity(
    config,
    request.communityActorUrl,
    builtCreate.activity,
  );
  const activityJson = await renderPublicActivityJson(activity);
  const objectId = builtCreate.objectId;
  const activityId = builtCreate.activityId;

  let deliveredRemoteSubscriberCount = 0;
  let failedRemoteSubscriberCount = 0;

  const debugDelivery = shouldLogSignedJsonDelivery(config);

  for (const remoteSubscriber of remoteSubscribers) {
    try {
      await sendSignedJsonActivity(
        "LocalCommunityPublish",
        remoteSubscriber.remoteInboxUrl,
        signingKey,
        activityJson,
        { debugDelivery },
      );
      console.log("[LocalCommunityPublish] delivery completed:", {
        remoteActorId: remoteSubscriber.remoteActorId,
        remoteInboxUrl: remoteSubscriber.remoteInboxUrl,
        signingKeyId: signingKey.keyId.href,
        deliveryBackend: "signed-json",
        debugDelivery,
      });
      deliveredRemoteSubscriberCount += 1;
    } catch (error) {
      // Fanout must continue toward healthy remote subscribers even if one target
      // rejects the activity or is temporarily unreachable.
      failedRemoteSubscriberCount += 1;
      console.error("[LocalCommunityPublish] signed JSON delivery failed:", {
        remoteActorId: remoteSubscriber.remoteActorId,
        remoteInboxUrl: remoteSubscriber.remoteInboxUrl,
        signingKeyId: signingKey.keyId.href,
        deliveryBackend: "signed-json",
        debugDelivery,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }

  if (deliveredRemoteSubscriberCount === 0) {
    throw new Error("Local community publish failed for all accepted remote subscribers");
  }

  const canonicalCreateJson = activityJson.object;
  if (canonicalCreateJson == null || typeof canonicalCreateJson !== "object" || Array.isArray(canonicalCreateJson)) {
    throw new Error("Local community Announce did not contain a canonical Create object");
  }
  return {
    activityId: activityId.href,
    objectId: objectId.href,
    communityActorUrl: request.communityActorUrl,
    deliveredFollowerCount: deliveredRemoteSubscriberCount,
    failedFollowerCount: failedRemoteSubscriberCount,
    activityJson: canonicalCreateJson as Record<string, unknown>,
  };
}

/** Build a local-community Announce envelope around an embedded user activity. */
export function buildLocalCommunityAnnounceActivity(
  config: GatewayConfig,
  communityActorUrl: string,
  embeddedActivity: Create | Update | Delete,
): Announce {
  const communityActor = new URL(communityActorUrl);
  const PUBLIC = new URL("https://www.w3.org/ns/activitystreams#Public");
  const announceId = buildGatewayUrl(
    config,
    `/actors/communities/${encodeURIComponent(communityActor.pathname.split("/").filter(Boolean).at(-1) ?? "community")}/activities/announce/posts/${Date.now()}-${Math.random().toString(36).slice(2)}`,
  );

  // The Announce is only a delivery envelope. User authorship and canonical
  // object identity stay in the embedded Create/Update/Delete activity, which
  // keeps Lemmy author display and Python's Discord mapping ids stable.
  return new Announce({
    id: announceId,
    actor: communityActor,
    object: embeddedActivity,
    tos: [PUBLIC],
    ccs: [new URL("followers", `${communityActor.href.replace(/\/$/, "")}/`)],
  });
}

/** Deliver one local-community mutation fanout when the actor has a local signing key. */
export async function maybeDeliverLocalCommunityMutation(
  config: GatewayConfig,
  communityActorUrl: string,
  activity: Update | Delete,
  targetInboxUrls: string[] | null | undefined,
): Promise<boolean> {
  /** Fan out one local-community mutation to accepted remote subscribers. */
  const signingKey = await loadLocalCommunitySigningKeyByActorUrl(config, communityActorUrl);
  if (signingKey == null) {
    return false;
  }

  const allRemoteSubscribers = await loadAcceptedRemoteSubscribersByActorUrl(config, communityActorUrl);
  const targetSet = targetInboxUrls == null ? null : new Set(targetInboxUrls);
  const remoteSubscribers = targetSet == null
    ? allRemoteSubscribers
    : allRemoteSubscribers.filter((subscriber) => targetSet.has(subscriber.remoteInboxUrl));
  const activityJson = await renderPublicActivityJson(
    buildLocalCommunityAnnounceActivity(config, communityActorUrl, activity),
  );
  const debugDelivery = shouldLogSignedJsonDelivery(config);

  for (const remoteSubscriber of remoteSubscribers) {
    // Mutation fanout remains per-target best-effort so one bad inbox does not
    // block every other accepted remote subscriber.
    await sendSignedJsonActivity(
      "LocalCommunityMutation",
      remoteSubscriber.remoteInboxUrl,
      signingKey,
      activityJson,
      { debugDelivery },
    );
  }
  return true;
}
