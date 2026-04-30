import {
  createFederation,
  InProcessMessageQueue,
  MemoryKvStore,
} from "@fedify/fedify";
import { Announce, Create, Endpoints, Follow } from "@fedify/vocab";
import { Service } from "@fedify/vocab";

import type { GatewayContextData } from "./config.js";
import { FileKeyStore } from "./key-store.js";
import { normalizeCreateActivity, normalizeCreateActivityFromJson } from "./normalize.js";
import { deliverEventToPythonBridge } from "./python-bridge.js";

export function createGatewayFederation(
  config: GatewayContextData,
  keyStore: FileKeyStore,
) {
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
      console.log("[Fedify] Received Create activity");
      const event = await normalizeCreateActivity(activity);
      if (event == null) {
        console.log("[Fedify] normalizeCreateActivity returned null");
        return;
      }
      console.log("[Fedify] Normalized event:", {
        event_type: event.event_type,
        community_actor_id: event.community_actor_id,
        config_community_actor_id: config.communityActorId,
      });
      if (
        config.communityActorId &&
        event.community_actor_id !== config.communityActorId
      ) {
        console.log("[Fedify] Event community does not match configured community, skipping");
        return;
      }

      console.log("[Fedify] Delivering event to Python bridge...");
      await deliverEventToPythonBridge(
        config.pythonBridgeEventsUrl,
        config.pythonBridgeSharedSecret,
        event,
      );
    })
    .on(Announce, async (ctx, activity) => {
      console.log("[Fedify] Received Announce activity");

      try {
        const rawJson = ctx.data.activitypubRawJson;
        const rawRecord =
          typeof rawJson === "object" && rawJson !== null
            ? (rawJson as Record<string, unknown>)
            : null;

        if (rawRecord) {
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
          console.log("[Fedify] Raw Announce shape:", {
            announceId: typeof rawRecord.id === "string" ? rawRecord.id : null,
            bodySha256: ctx.data.activitypubRawBodySha256 ?? null,
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
        } else {
          console.log("[Fedify] Raw Announce shape: no object payload");
        }

        if (
          rawRecord &&
          typeof rawRecord.object === "object" &&
          rawRecord.object !== null &&
          "type" in rawRecord.object &&
          rawRecord.object.type === "Create"
        ) {
          const createActivity = rawRecord.object;
          console.log("[Fedify] Got Create activity from raw JSON");

          const event = normalizeCreateActivityFromJson(createActivity);
          if (event == null) {
            console.log("[Fedify] normalizeCreateActivityFromJson returned null");
            return;
          }

          console.log("[Fedify] Normalized event:", {
            event_type: event.event_type,
            community_actor_id: event.community_actor_id,
          });

          if (
            config.communityActorId &&
            event.community_actor_id !== config.communityActorId
          ) {
            console.log("[Fedify] Event community does not match, skipping");
            return;
          }

          console.log("[Fedify] Delivering event to Python bridge...");
          await deliverEventToPythonBridge(
            config.pythonBridgeEventsUrl,
            config.pythonBridgeSharedSecret,
            event,
          );
          console.log("[Fedify] Event delivered successfully");
        } else {
          console.log("[Fedify] Could not find Create in Announce.object from raw JSON");
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
