/**
 * Registered User Actor delivery batch transport.
 *
 * Python owns durable endpoint grouping, retry, and outcome persistence. The
 * gateway only verifies the local actor, loads that actor's signing key at the
 * delivery boundary, signs each explicit activity JSON, and returns independent
 * per-delivery outcomes.
 */

import type { GatewayConfig } from "../config.js";
import { loadActorKeyPair, loadUserActorIdentity } from "../actor-store.js";
import type { SendUserActorDeliveriesRequest } from "../types.js";
import { sendSignedJsonActivity, shouldLogSignedJsonDelivery } from "./signed-json-delivery.js";

/** Sign and deliver one explicit User Actor endpoint batch with independent outcomes. */
export async function sendUserActorDeliveries(
  config: GatewayConfig,
  request: SendUserActorDeliveriesRequest,
): Promise<{ outcomes: Array<{ deliveryId: number; ok: boolean; error: string | null }> }> {
  // Stage 4 keeps durable retry ownership in Python. The gateway only resolves
  // the canonical local key and signs each explicit endpoint independently.
  const identity = await loadUserActorIdentity(config, request.actorUsername);
  if (identity == null || identity.actorId.href !== request.signingActorUrl) {
    throw new Error("actorUsername and signingActorUrl must identify the same local User Actor");
  }
  const keyPair = await loadActorKeyPair(config, request.actorUsername);
  if (keyPair == null) throw new Error("User Actor signing key not found");
  const signingKey = { keyId: new URL("#main-key", identity.actorId), privateKey: keyPair.privateKey };
  const outcomes = [];
  for (const delivery of request.deliveries) {
    if (delivery.activityJson.actor !== request.signingActorUrl) {
      outcomes.push({ deliveryId: delivery.deliveryId, ok: false, error: "activity.actor must match signingActorUrl" });
      continue;
    }
    try {
      await sendSignedJsonActivity(
        "UserActorDelivery",
        delivery.targetEndpointUrl,
        signingKey,
        delivery.activityJson,
        {
          debugDelivery: shouldLogSignedJsonDelivery(config),
          includeRequestDetailsInDiagnostics: false,
          includeResponseBodyInDiagnostics: false,
        },
      );
      outcomes.push({ deliveryId: delivery.deliveryId, ok: true, error: null });
    } catch (error) {
      outcomes.push({
        deliveryId: delivery.deliveryId,
        ok: false,
        error: (error instanceof Error ? error.message : String(error)).slice(0, 500),
      });
    }
  }
  return { outcomes };
}
