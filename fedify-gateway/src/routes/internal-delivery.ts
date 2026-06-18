/**
 * Internal delivery route registration.
 *
 * These handlers preserve existing batch validation and delivery handoff while
 * moving operational endpoint bodies out of the gateway composition file.
 */
import type { Hono } from "hono";

import type { GatewayConfig } from "../config.js";
import type { createGatewayFederation } from "../federation.js";
import { sendLocalCommunityRelay, sendUserActorDeliveries } from "../federation-outbound.js";
import type { SendLocalCommunityRelayRequest, SendUserActorDeliveriesRequest } from "../types.js";
import { hasValidInternalAuthorization } from "./internal-auth.js";

export interface InternalDeliveryRouteDependencies {
  config: GatewayConfig;
  fedify: ReturnType<typeof createGatewayFederation>;
}

/** Mount internal delivery batch endpoints with unchanged request validation. */
export function registerInternalDeliveryRoutes(
  app: Hono,
  dependencies: InternalDeliveryRouteDependencies,
): void {
  const { config, fedify } = dependencies;

  app.post("/send-local-community-relay", async (context) => {
    if (!hasValidInternalAuthorization(config, context.req.header("Authorization") ?? null)) {
      return context.json({ error: "invalid authorization" }, { status: 401 });
    }

    const payload = (await context.req.json()) as Partial<SendLocalCommunityRelayRequest>;
    if (typeof payload.signingActorUrl !== "string" || !Array.isArray(payload.deliveries)) {
      return context.json({ error: "signingActorUrl and deliveries are required" }, { status: 400 });
    }
    for (const delivery of payload.deliveries) {
      if (
        typeof delivery.deliveryId !== "number" ||
        typeof delivery.targetRemoteActorId !== "string" ||
        typeof delivery.targetInboxUrl !== "string" ||
        delivery.activityJson == null ||
        typeof delivery.activityJson !== "object" ||
        Array.isArray(delivery.activityJson)
      ) {
        return context.json(
          { error: "each delivery requires deliveryId, targetRemoteActorId, targetInboxUrl, and activityJson" },
          { status: 400 },
        );
      }
    }

    try {
      const result = await sendLocalCommunityRelay(fedify, config, {
        signingActorUrl: payload.signingActorUrl,
        deliveries: payload.deliveries,
      });
      return context.json(result);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return context.json({ error: message }, { status: 500 });
    }
  });

  app.post("/send-user-actor-deliveries", async (context) => {
    if (!hasValidInternalAuthorization(config, context.req.header("Authorization") ?? null)) {
      return context.json({ error: "invalid authorization" }, { status: 401 });
    }
    const payload = (await context.req.json()) as Partial<SendUserActorDeliveriesRequest>;
    if (
      typeof payload.signingActorUrl !== "string" ||
      typeof payload.actorUsername !== "string" ||
      !Array.isArray(payload.deliveries)
    ) {
      return context.json({ error: "invalid User Actor delivery request" }, { status: 400 });
    }
    try {
      const request = payload as SendUserActorDeliveriesRequest;
      const endpointKeys = new Set<string>();
      for (const delivery of request.deliveries) {
        const key = `${request.signingActorUrl}\n${delivery.targetEndpointUrl}`;
        if (endpointKeys.has(key)) {
          return context.json(
            { error: "a gateway batch may contain only one User Actor version per endpoint" },
            { status: 400 },
          );
        }
        endpointKeys.add(key);
      }
      return context.json(await sendUserActorDeliveries(config, request));
    } catch (error) {
      return context.json({ error: error instanceof Error ? error.message : String(error) }, { status: 500 });
    }
  });
}
