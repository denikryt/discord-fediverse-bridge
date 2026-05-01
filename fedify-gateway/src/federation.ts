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
        if (isDebug) {
          console.log("[Fedify][debug] normalizeCreateActivity returned null");
        }
        return;
      }
      if (
        config.communityActorId &&
        event.community_actor_id !== config.communityActorId
      ) {
        if (isDebug) {
          console.log("[Fedify][debug] Event community does not match configured community, skipping");
        }
        return;
      }

      console.log("[Fedify] Delivering event", {
        eventType: event.event_type,
        deliveryId: event.delivery_id,
        objectId: event.object.ap_id,
      });
      await deliverEventToPythonBridge(
        config.pythonBridgeEventsUrl,
        config.pythonBridgeSharedSecret,
        event,
      );
    })
    .on(Announce, async (ctx, activity) => {
      try {
        const announceId = activity.id?.href ?? null;
        const cachedRaw = announceId ? getRawActivity(announceId) : null;
        const rawJson = cachedRaw?.rawJson ?? ctx.data.activitypubRawJson;
        const rawBodySha256 = cachedRaw?.rawBodySha256 ?? ctx.data.activitypubRawBodySha256;
        const rawRecord =
          typeof rawJson === "object" && rawJson !== null
            ? (rawJson as Record<string, unknown>)
            : null;

        if (rawRecord && isDebug) {
          const rawObject =
            typeof rawRecord.object === "object" && rawRecord.object !== null
              ? (rawRecord.object as Record<string, unknown>)
              : null;
          const nestedObject =
            rawObject &&
            typeof rawObject.object === "object" &&
            rawObject.object !== null
              ? (rawObject.object as Record<string, unknown>)
              : null;
          console.log("[Fedify][debug] Raw Announce shape:", {
            announceId: typeof rawRecord.id === "string" ? rawRecord.id : null,
            bodySha256: rawBodySha256 ?? null,
            keys: Object.keys(rawRecord),
            objectType:
              rawObject && typeof rawObject.type === "string"
                ? rawObject.type
                : typeof rawRecord.object,
            objectKeys: rawObject ? Object.keys(rawObject) : null,
            createId:
              rawObject && typeof rawObject.id === "string" ? rawObject.id : null,
            nestedObjectId:
              nestedObject && typeof nestedObject.id === "string"
                ? nestedObject.id
                : null,
            nestedObjectType:
              nestedObject && typeof nestedObject.type === "string"
                ? nestedObject.type
                : null,
          });
        } else if (isDebug) {
          console.log("[Fedify][debug] Raw Announce shape: no object payload");
        }

        if (
          rawRecord &&
          typeof rawRecord.object === "object" &&
          rawRecord.object !== null &&
          "type" in rawRecord.object &&
          rawRecord.object.type === "Create"
        ) {
          const createActivity = rawRecord.object;

          const event = normalizeCreateActivityFromJson(createActivity);
          if (event == null) {
            if (isDebug) {
              console.log("[Fedify][debug] normalizeCreateActivityFromJson returned null");
            }
            return;
          }

          if (
            config.communityActorId &&
            event.community_actor_id !== config.communityActorId
          ) {
            if (isDebug) {
              console.log("[Fedify][debug] Event community does not match, skipping");
            }
            return;
          }

          const createRecord = createActivity as Record<string, unknown>;
          const nestedObject =
            typeof createRecord.object === "object" && createRecord.object !== null
              ? (createRecord.object as Record<string, unknown>)
              : null;
          console.log("[Fedify] Delivering event", {
            announceId: typeof rawRecord.id === "string" ? rawRecord.id : null,
            createId:
              typeof createRecord.id === "string" ? createRecord.id : event.delivery_id,
            eventType: event.event_type,
            kind: event.object.kind,
            objectId:
              nestedObject && typeof nestedObject.id === "string"
                ? nestedObject.id
                : event.object.ap_id,
          });
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
        } else {
          if (isDebug) {
            console.log("[Fedify][debug] Could not find Create in Announce.object from raw JSON");
          }
        }
      } catch (error) {
        console.error("[Fedify] Error processing Announce:", error instanceof Error ? error.message : error);
      }
    })
    .on(Follow, async () => {
      return;
    });

  return federation;
}
