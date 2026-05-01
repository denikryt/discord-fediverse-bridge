import {
  createFederation,
  InProcessMessageQueue,
  MemoryKvStore,
} from "@fedify/fedify";
import { Announce, Create, Endpoints, Follow } from "@fedify/vocab";
import { Service } from "@fedify/vocab";

import { getRawActivity } from "./activitypub-raw-cache.js";
import type { GatewayContextData } from "./config.js";
import { FileKeyStore } from "./key-store.js";
import { normalizeCreateActivity, normalizeCreateActivityFromJson } from "./normalize.js";
import { deliverEventToPythonBridge } from "./python-bridge.js";
import type { BridgeEvent } from "./types.js";

export function createGatewayFederation(
  config: GatewayContextData,
  keyStore: FileKeyStore,
) {
  const isDebug = config.logLevel === "debug";
  const federation = createFederation<GatewayContextData>({
    origin: config.fedifyOrigin,
    kv: new MemoryKvStore(),
    queue: new InProcessMessageQueue(),
  });

  federation
    .setActorDispatcher("/actors/{identifier}", async (ctx, identifier) => {
      if (identifier !== config.actorIdentifier) {
        return null;
      }

      return new Service({
        id: ctx.getActorUri(identifier),
        preferredUsername: identifier,
        name: config.actorName,
        summary: config.actorSummary,
        inbox: ctx.getInboxUri(identifier),
        outbox: new URL(`${config.fedifyOrigin}actors/${identifier}/outbox`),
        endpoints: new Endpoints({
          sharedInbox: ctx.getInboxUri(),
        }),
        publicKeys: (await ctx.getActorKeyPairs(identifier)).map(
          (keyPair) => keyPair.cryptographicKey,
        ),
      });
    })
    .setKeyPairsDispatcher(async (_ctx, identifier) => {
      if (identifier !== config.actorIdentifier) {
        return [];
      }
      return [await keyStore.getOrCreate(identifier)];
    })
    .mapHandle(async (_ctx, username) => {
      return username === config.actorIdentifier ? username : null;
    });

  federation
    .setInboxListeners("/actors/{identifier}/inbox", "/inbox")
    .withIdempotency("per-inbox")
    .on(Create, async (_ctx, activity) => {
      if (isDebug) {
        console.log("[Fedify][debug] Received direct Create activity");
      }
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

        const event = normalizeCreateActivityFromJson(createRecord);
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
