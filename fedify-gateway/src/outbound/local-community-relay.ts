/**
 * Explicit local-community relay delivery for already-rendered activities.
 *
 * Python owns target selection and exact activity JSON. The gateway only checks
 * actor/signing-key consistency, signs each target request, and returns one
 * outcome per requested delivery.
 */

import type { Federation } from "@fedify/fedify";

import type { GatewayConfig } from "../config.js";
import { loadLocalCommunitySigningKeyByActorUrl } from "../local-community-keys.js";
import type { SendLocalCommunityRelayRequest, SendLocalCommunityRelayResult } from "../types.js";
import { sendSignedJsonActivity, shouldLogSignedJsonDelivery } from "./signed-json-delivery.js";

/** Sign and relay explicit already-rendered local-community activities. */
export async function sendLocalCommunityRelay(
  federation: Federation<GatewayConfig>,
  config: GatewayConfig,
  request: SendLocalCommunityRelayRequest,
): Promise<SendLocalCommunityRelayResult> {
  // Python has already selected targets and rendered exact ActivityPub JSON.
  // The gateway only signs as the requested local community actor and delivers
  // to the explicit inboxes, preserving the policy/transport boundary.
  const signingActor = new URL(request.signingActorUrl);
  const signingKey = await loadLocalCommunitySigningKeyByActorUrl(
    config,
    request.signingActorUrl,
  );
  if (signingKey == null) {
    throw new Error("signingActorUrl does not identify a local community actor");
  }
  if (signingKey.actorId.href !== signingActor.href) {
    throw new Error("signingActorUrl must match the canonical community actor URL");
  }
  const outcomes = [];
  const debugDelivery = shouldLogSignedJsonDelivery(config);

  for (const delivery of request.deliveries) {
    const activityActor = delivery.activityJson.actor;
    if (activityActor !== request.signingActorUrl) {
      outcomes.push({
        deliveryId: delivery.deliveryId,
        targetRemoteActorId: delivery.targetRemoteActorId,
        ok: false,
        activityId: typeof delivery.activityJson.id === "string" ? delivery.activityJson.id : null,
        error: "activity.actor must match signingActorUrl",
      });
      continue;
    }

    try {
      await sendSignedJsonActivity(
        "LocalCommunityRelay",
        delivery.targetInboxUrl,
        signingKey,
        delivery.activityJson,
        { debugDelivery },
      );
      outcomes.push({
        deliveryId: delivery.deliveryId,
        targetRemoteActorId: delivery.targetRemoteActorId,
        ok: true,
        activityId: typeof delivery.activityJson.id === "string" ? delivery.activityJson.id : null,
        error: null,
      });
    } catch (error) {
      outcomes.push({
        deliveryId: delivery.deliveryId,
        targetRemoteActorId: delivery.targetRemoteActorId,
        ok: false,
        activityId: typeof delivery.activityJson.id === "string" ? delivery.activityJson.id : null,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }

  return { outcomes };
}
