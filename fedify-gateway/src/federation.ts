import {
  createFederation,
  InProcessMessageQueue,
  MemoryKvStore,
} from "@fedify/fedify";
import { Announce, Create, Follow } from "@fedify/vocab";

import { getRawActivity } from "./activitypub-raw-cache.js";
import {
  buildBridgeServiceActor,
  buildUserPersonActor,
} from "./actors.js";
import {
  getBridgeActorIdentity,
  hasLocalActor,
  loadActorKeyPair,
  loadUserActorIdentity,
} from "./actor-store.js";
import type { GatewayContextData } from "./config.js";
import { normalizeCreateActivity, normalizeCreateActivityFromJson } from "./normalize.js";
import { deliverEventToPythonBridge } from "./python-bridge.js";
import type { BridgeEvent } from "./types.js";

export function createGatewayFederation(
  config: GatewayContextData,
) {
  // The federation definition keeps protocol-specific concerns in one place so
  // the rest of the gateway can stay focused on normalization and delivery.
  const isDebug = config.logLevel === "debug";
  const federation = createFederation<GatewayContextData>({
    origin: config.fedifyOrigin,
    kv: new MemoryKvStore(),
    queue: new InProcessMessageQueue(),
  });

  federation
    .setActorDispatcher("/actors/{identifier}", async (ctx, identifier) => {
      const sharedInboxId = ctx.getInboxUri();

      if (identifier === config.actorIdentifier) {
        const bridgeIdentity = getBridgeActorIdentity(config);
        const bridgeKeys = await ctx.getActorKeyPairs(identifier);
        return buildBridgeServiceActor(
          bridgeIdentity,
          sharedInboxId,
          bridgeKeys,
        );
      }

      const userIdentity = await loadUserActorIdentity(config, identifier);
      if (userIdentity == null) {
        return null;
      }

      // User actors remain Person objects even though the actor dispatcher path
      // is shared with the bridge actor. Their canonical IDs come from the
      // Python-owned registration records in the shared database.
      return buildUserPersonActor(
        userIdentity,
        sharedInboxId,
        await ctx.getActorKeyPairs(identifier),
      );
    })
    .setKeyPairsDispatcher(async (_ctx, identifier) => {
      const keyPair = await loadActorKeyPair(config, identifier);
      if (keyPair == null) {
        return [];
      }
      return [keyPair];
    })
    .mapHandle(async (_ctx, username) => {
      if (!(await hasLocalActor(config, username))) {
        return null;
      }
      return username;
    })
    .mapAlias(async (_ctx, resource) => {
      if (
        resource.href ===
        new URL(`/actors/${config.actorIdentifier}`, config.fedifyOrigin).href
      ) {
        return { identifier: config.actorIdentifier };
      }

      const username = parseUserAlias(resource, config.fedifyOrigin);
      if (username == null || !(await hasLocalActor(config, username))) {
        return null;
      }
      return { username };
    });

  federation
    .setInboxListeners("/users/{identifier}/inbox", "/inbox")
    .withIdempotency("per-inbox")
    .on(Create, async (_ctx, activity) => {
      if (isDebug) {
        console.log("[Fedify][debug] Received direct Create activity");
      }
      // Direct Create handling is the happy path when the remote server does
      // not wrap local objects inside Announce.
      const event = await normalizeCreateActivity(activity);
      if (event == null) {
        logDebug(isDebug, "normalizeCreateActivity returned null");
        return;
      }
      if (shouldSkipCommunityEvent(event, config.communityActorId)) {
        logDebug(
          isDebug,
          "Event community does not match configured community, skipping",
        );
        return;
      }

      await deliverNormalizedEvent(config, event, {
        deliveryId: event.delivery_id,
        eventType: event.event_type,
        objectId: event.object.ap_id,
      });
    })
    .on(Announce, async (ctx, activity) => {
      try {
        // Lemmy commonly sends Announce(Create(...)), so this path unwraps the
        // nested Create using the cached raw inbox payload.
        const announceEnvelope = getAnnounceEnvelope(ctx, activity.id?.href ?? null);

        logAnnounceDebug(isDebug, announceEnvelope);

        const createRecord = extractCreateRecord(announceEnvelope.rawRecord);
        if (createRecord == null) {
          logDebug(
            isDebug,
            "Could not find Create in Announce.object from raw JSON",
          );
          return;
        }

        const event = await normalizeCreateActivityFromJson(createRecord);
        if (event == null) {
          logDebug(isDebug, "normalizeCreateActivityFromJson returned null");
          return;
        }

        if (shouldSkipCommunityEvent(event, config.communityActorId)) {
          logDebug(isDebug, "Event community does not match, skipping");
          return;
        }

        const nestedObject = asRecord(createRecord.object);
        await deliverNormalizedEvent(config, event, {
          announceId: announceEnvelope.announceId,
          createId: asString(createRecord.id) ?? event.delivery_id,
          eventType: event.event_type,
          kind: event.object.kind,
          objectId: asString(nestedObject?.id) ?? event.object.ap_id,
        });
      } catch (error) {
        console.error(
          "[Fedify] Error processing Announce:",
          error instanceof Error ? error.message : error,
        );
      }
    })
    .on(Follow, async () => {
      return;
    });

  return federation;
}

async function deliverNormalizedEvent(
  config: GatewayContextData,
  event: BridgeEvent,
  logContext: Record<string, unknown>,
): Promise<void> {
  // All successful normalization funnels through one delivery path so logging
  // and Python-bridge auth stay consistent across Create and Announce.
  console.log("[Fedify] Delivering event", logContext);
  await deliverEventToPythonBridge(
    config.pythonBridgeEventsUrl,
    config.pythonBridgeSharedSecret,
    event,
  );
  console.log("[Fedify] Event delivered", {
    deliveryId: event.delivery_id,
    kind: event.object.kind,
    objectId: event.object.ap_id,
  });
}

function shouldSkipCommunityEvent(
  event: BridgeEvent,
  communityActorId: string | null,
): boolean {
  return Boolean(
    communityActorId && event.community_actor_id !== communityActorId,
  );
}

function getAnnounceEnvelope(
  ctx: { data: GatewayContextData },
  announceId: string | null,
): {
  announceId: string | null;
  rawRecord: Record<string, unknown> | null;
  rawBodySha256: string | undefined;
} {
  // Prefer the payload stored at HTTP-ingest time because queued handler
  // execution may no longer have the original request-local body attached.
  const cachedRaw = announceId ? getRawActivity(announceId) : null;
  const rawJson = cachedRaw?.rawJson ?? ctx.data.activitypubRawJson;
  return {
    announceId,
    rawRecord: asRecord(rawJson),
    rawBodySha256:
      cachedRaw?.rawBodySha256 ?? ctx.data.activitypubRawBodySha256,
  };
}

function logAnnounceDebug(
  isDebug: boolean,
  announceEnvelope: {
    announceId: string | null;
    rawRecord: Record<string, unknown> | null;
    rawBodySha256: string | undefined;
  },
): void {
  if (!isDebug) {
    return;
  }
  const { rawRecord, rawBodySha256 } = announceEnvelope;
  if (rawRecord == null) {
    console.log("[Fedify][debug] Raw Announce shape: no object payload");
    return;
  }

  const rawObject = asRecord(rawRecord.object);
  const nestedObject = asRecord(rawObject?.object);
  console.log("[Fedify][debug] Raw Announce shape:", {
    announceId: asString(rawRecord.id),
    bodySha256: rawBodySha256 ?? null,
    keys: Object.keys(rawRecord),
    objectType: asString(rawObject?.type) ?? typeof rawRecord.object,
    objectKeys: rawObject ? Object.keys(rawObject) : null,
    createId: asString(rawObject?.id),
    nestedObjectId: asString(nestedObject?.id),
    nestedObjectType: asString(nestedObject?.type),
  });
}

function extractCreateRecord(
  rawRecord: Record<string, unknown> | null,
): Record<string, unknown> | null {
  const rawObject = asRecord(rawRecord?.object);
  return rawObject?.type === "Create" ? rawObject : null;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function logDebug(isDebug: boolean, message: string): void {
  if (isDebug) {
    console.log(`[Fedify][debug] ${message}`);
  }
}

function parseUserAlias(resource: URL, origin: string): string | null {
  // User aliases are limited to the canonical `/users/{username}` surface so
  // the manual user-actor routes and Fedify alias resolution stay consistent.
  const resourceOrigin = new URL(origin);
  if (resource.origin !== resourceOrigin.origin) {
    return null;
  }

  const parts = resource.pathname.split("/").filter(Boolean);
  if (parts.length !== 2 || parts[0] !== "users") {
    return null;
  }
  return parts[1];
}
