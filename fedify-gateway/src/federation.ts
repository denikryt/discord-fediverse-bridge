import {
  createFederation,
  InProcessMessageQueue,
  MemoryKvStore,
} from "@fedify/fedify";
import { Accept, Announce, Create, Delete, Follow, Undo, Update } from "@fedify/vocab";

import { getRawActivity } from "./activitypub-raw-cache.js";
import {
  buildBridgeServiceActor,
  buildLocalCommunityGroupActor,
  buildUserPersonActor,
} from "./actors.js";
import {
  getBridgeActorIdentity,
  hasLocalActor,
  loadActorKeyPair,
  loadLocalCommunityIdentity,
  resolveLocalCommunityByActorUrl,
  resolveLocalActorKind,
  loadUserActorIdentity,
} from "./actor-store.js";
import { buildLocalCommunityPublicKeyCarrier } from "./local-community-keys.js";
import type { GatewayContextData } from "./config.js";
import {
  normalizeCreateActivity,
  normalizeCreateActivityFromJson,
  normalizeUpdateActivityFromJson,
  normalizeDeleteActivityFromJson,
} from "./normalize.js";
import { getPythonBridgeClient } from "./python-bridge-client.js";
import type {
  BridgeContentEvent,
  FollowAcceptedEvent,
  LocalFollowRequestedEvent,
  LocalUnfollowRequestedEvent,
} from "./types.js";

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
      const actorKind = await resolveLocalActorKind(config, identifier);

      if (actorKind === "bridge") {
        const bridgeIdentity = getBridgeActorIdentity(config);
        const bridgeKeys = await ctx.getActorKeyPairs(identifier);
        return buildBridgeServiceActor(
          bridgeIdentity,
          sharedInboxId,
          bridgeKeys,
        );
      }

      if (actorKind === "user") {
        const userIdentity = await loadUserActorIdentity(config, identifier);
        if (userIdentity == null) {
          return null;
        }

        // User actors remain Person objects and now use the dispatcher route as
        // their canonical ActivityPub identity. This keeps actor IDs and Fedify
        // key ownership aligned for Mastodon/Lemmy fetch-time validation.
        return buildUserPersonActor(
          userIdentity,
          sharedInboxId,
          await ctx.getActorKeyPairs(identifier),
        );
      }

      if (actorKind === "community") {
        const communityIdentity = await loadLocalCommunityIdentity(config, identifier);
        if (communityIdentity == null) {
          return null;
        }
        return buildLocalCommunityGroupActor(
          communityIdentity,
          sharedInboxId,
          await buildLocalCommunityPublicKeyCarrier(communityIdentity),
        );
      }

      return null;
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
    .setInboxListeners("/communities/{identifier}/inbox", "/inbox")
    .withIdempotency("per-inbox")
    .on(Create, async (_ctx, activity) => {
      if (isDebug) {
        console.log("[Fedify][debug] Received direct Create activity");
      }
      try {
        // Direct Create handling is the happy path when the remote server does
        // not wrap local objects inside Announce. Errors must be logged here
        // because the inbox already returns 202 before async processing finishes.
        const event = await normalizeCreateActivity(activity, {
          pythonBridgeClient: getPythonBridgeClient(config),
        });
        if (event == null) {
          logDebug(isDebug, "normalizeCreateActivity returned null");
          return;
        }

        const sourceActivityJson = await activity.toJsonLd() as Record<string, unknown>;
        event.source_activity_json = sourceActivityJson;
        event.source_activity_id = typeof sourceActivityJson.id === "string" ? sourceActivityJson.id : event.delivery_id;
        event.source_announce_id = null;
        await deliverNormalizedEvent(config, event, {
          deliveryId: event.delivery_id,
          eventType: event.event_type,
          objectId: event.object.ap_id,
        });
      } catch (error) {
        console.error("[Fedify] Error processing direct Create activity", error);
      }
    })
    .on(Update, async (_ctx, activity) => {
      try {
        // Direct Update must be forwarded even when the remote server does not
        // wrap it inside Announce.
        await deliverDirectUpdateActivity(config, activity, isDebug);
      } catch (error) {
        console.error("[Fedify] Error processing direct Update activity", error);
      }
    })
    .on(Delete, async (_ctx, activity) => {
      try {
        // Direct Delete is the delete counterpart to direct Create/Update.
        await deliverDirectDeleteActivity(config, activity, isDebug);
      } catch (error) {
        console.error("[Fedify] Error processing direct Delete activity", error);
      }
    })
    .on(Announce, async (ctx, activity) => {
      try {
        // Lemmy wraps Create, Update, and Delete inside Announce.
        // Try each type in turn using the cached raw inbox payload.
        const announceEnvelope = getAnnounceEnvelope(ctx, activity.id?.href ?? null);

        logAnnounceDebug(isDebug, announceEnvelope);

        const createRecord = extractCreateRecord(announceEnvelope.rawRecord);
        if (createRecord != null) {
          const event = await normalizeCreateActivityFromJson(createRecord, {
            pythonBridgeClient: getPythonBridgeClient(config),
          });
          if (event == null) {
            logDebug(isDebug, "normalizeCreateActivityFromJson returned null");
            return;
          }
          const nestedObject = asRecord(createRecord.object);
          event.source_activity_json = createRecord;
          event.source_activity_id = asString(createRecord.id) ?? event.delivery_id;
          event.source_announce_id = announceEnvelope.announceId;
          await deliverNormalizedEvent(config, event, {
            announceId: announceEnvelope.announceId,
            createId: asString(createRecord.id) ?? event.delivery_id,
            eventType: event.event_type,
            kind: event.object.kind,
            objectId: asString(nestedObject?.id) ?? event.object.ap_id,
          });
          return;
        }

        const updateRecord = extractUpdateRecord(announceEnvelope.rawRecord);
        if (updateRecord != null) {
          const event = await normalizeUpdateActivityFromJson(updateRecord, {
            pythonBridgeClient: getPythonBridgeClient(config),
          });
          if (event == null) {
            logDebug(isDebug, "normalizeUpdateActivityFromJson returned null");
            return;
          }
          event.source_activity_json = updateRecord;
          event.source_activity_id = asString(updateRecord.id) ?? event.delivery_id;
          event.source_announce_id = announceEnvelope.announceId;
          await deliverNormalizedEvent(config, event, {
            announceId: announceEnvelope.announceId,
            updateId: asString(updateRecord.id) ?? event.delivery_id,
            eventType: event.event_type,
            objectId: event.object.ap_id,
          });
          return;
        }

        const deleteRecord = extractDeleteRecord(announceEnvelope.rawRecord);
        if (deleteRecord != null) {
          const event = await normalizeDeleteActivityFromJson(deleteRecord, { pythonBridgeClient: getPythonBridgeClient(config) });
          if (event == null) {
            logDebug(isDebug, "normalizeDeleteActivityFromJson returned null");
            return;
          }
          event.source_activity_json = deleteRecord;
          event.source_activity_id = asString(deleteRecord.id) ?? event.delivery_id;
          event.source_announce_id = announceEnvelope.announceId;
          await deliverNormalizedEvent(config, event, {
            announceId: announceEnvelope.announceId,
            deleteId: asString(deleteRecord.id) ?? event.delivery_id,
            eventType: event.event_type,
            objectId: event.object.ap_id,
          });
          return;
        }

        logDebug(
          isDebug,
          "Could not find Create, Update, or Delete in Announce.object from raw JSON",
        );
      } catch (error) {
        console.error(
          "[Fedify] Error processing Announce:",
          error instanceof Error ? error.message : error,
        );
      }
    })
    .on(Follow, async (_ctx, activity) => {
      const event = await buildLocalFollowRequestedEvent(config, activity);
      if (event == null) {
        return;
      }
      await deliverNormalizedEvent(config, event, {
        deliveryId: event.delivery_id,
        eventType: event.event_type,
        communityActorUrl: event.target_actor_id,
        remoteActorId: event.actor_id,
      });
    })
    .on(Undo, async (ctx, activity) => {
      const event = await buildLocalUnfollowRequestedEvent(
        config,
        activity,
        asRecord(ctx.data.activitypubRawJson),
      );
      if (event == null) {
        return;
      }
      await deliverNormalizedEvent(config, event, {
        deliveryId: event.delivery_id,
        eventType: event.event_type,
        communityActorUrl: event.target_actor_id,
        remoteActorId: event.actor_id,
        followActivityId: event.object.follow_activity_id,
      });
    })
    .on(Accept, async (ctx, activity) => {
      const activityId = activity.id?.href ?? null;
      const rawJson = ctx.data.activitypubRawJson;
      console.log("[Fedify][debug] Accept received", {
        activityId,
        actorId: activity.actorId?.href,
        objectId: activity.objectId?.href,
        rawObject: (rawJson as Record<string, unknown>)?.object,
      });
      const event = buildFollowAcceptedEvent(
        activity,
        ctx.data,
        activityId,
      );
      if (event == null) {
        logDebug(isDebug, "Accept did not contain a follow activity id");
        console.log("[Fedify][debug] Accept null event — raw activity:", JSON.stringify(rawJson, null, 2));
        return;
      }

      await deliverNormalizedEvent(config, event, {
        deliveryId: event.delivery_id,
        eventType: event.event_type,
        followActivityId: event.object.follow_activity_id,
      });
    });

  return federation;
}

async function deliverNormalizedEvent(
  config: GatewayContextData,
  event: BridgeContentEvent | FollowAcceptedEvent | LocalFollowRequestedEvent | LocalUnfollowRequestedEvent,
  logContext: Record<string, unknown>,
): Promise<void> {
  // All successful normalization funnels through one delivery path so logging
  // and Python-bridge auth stay consistent across Create and Announce.
  if (config.logLevel === "debug") {
    console.log("[Fedify][debug] Delivering event", logContext);
  }
  await getPythonBridgeClient(config).deliverEvent(event);
}

export async function deliverDirectUpdateActivity(
  config: GatewayContextData,
  activity: Update,
  isDebug = false,
): Promise<void> {
  /** Normalize one direct Update and deliver it to the Python bridge. */
  const sourceActivityJson = await activity.toJsonLd() as Record<string, unknown>;
  const event = await normalizeUpdateActivityFromJson(sourceActivityJson, {
    pythonBridgeClient: getPythonBridgeClient(config),
  });
  if (event == null) {
    logDebug(isDebug, "normalizeUpdateActivityFromJson returned null for direct Update");
    return;
  }
  event.source_activity_json = sourceActivityJson;
  event.source_activity_id = typeof sourceActivityJson.id === "string" ? sourceActivityJson.id : event.delivery_id;
  event.source_announce_id = null;
  await deliverNormalizedEvent(config, event, {
    deliveryId: event.delivery_id,
    eventType: event.event_type,
    objectId: event.object.ap_id,
  });
}

export async function deliverDirectDeleteActivity(
  config: GatewayContextData,
  activity: Delete,
  isDebug = false,
): Promise<void> {
  /** Normalize one direct Delete and deliver it to the Python bridge. */
  const sourceActivityJson = await activity.toJsonLd() as Record<string, unknown>;
  const event = await normalizeDeleteActivityFromJson(sourceActivityJson, { pythonBridgeClient: getPythonBridgeClient(config) });
  if (event == null) {
    logDebug(isDebug, "normalizeDeleteActivityFromJson returned null for direct Delete");
    return;
  }
  event.source_activity_json = sourceActivityJson;
  event.source_activity_id = typeof sourceActivityJson.id === "string" ? sourceActivityJson.id : event.delivery_id;
  event.source_announce_id = null;
  await deliverNormalizedEvent(config, event, {
    deliveryId: event.delivery_id,
    eventType: event.event_type,
    objectId: event.object.ap_id,
  });
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

function extractUpdateRecord(
  rawRecord: Record<string, unknown> | null,
): Record<string, unknown> | null {
  // Lemmy sends Announce(Update(...)) for post and comment edits.
  const rawObject = asRecord(rawRecord?.object);
  return rawObject?.type === "Update" ? rawObject : null;
}

function extractDeleteRecord(
  rawRecord: Record<string, unknown> | null,
): Record<string, unknown> | null {
  // Lemmy sends Announce(Delete(...)) for post and comment deletes.
  const rawObject = asRecord(rawRecord?.object);
  return rawObject?.type === "Delete" ? rawObject : null;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function asUrlString(value: unknown): string | null {
  if (typeof value === "string") {
    return value;
  }
  if (value instanceof URL) {
    return value.href;
  }
  return null;
}

function logDebug(isDebug: boolean, message: string): void {
  if (isDebug) {
    console.log(`[Fedify][debug] ${message}`);
  }
}

export function buildFollowAcceptedEvent(
  activity: Accept,
  data: GatewayContextData,
  fallbackDeliveryId: string | null,
): FollowAcceptedEvent | null {
  // The typed Accept activity is the source of truth for follow lifecycle
  // events. Raw JSON exists only as a fallback because Hono/Fedify request
  // body handling can be unavailable when the internal queue re-dispatches.
  const rawJson = data.activitypubRawJson
    ?? (fallbackDeliveryId != null ? getRawActivity(fallbackDeliveryId)?.rawJson : undefined);
  const rawRecord = asRecord(rawJson);
  const actorId = activity.actorId?.href ?? asString(rawRecord?.actor);
  const objectValue = rawRecord?.object;
  const objectRecord = asRecord(objectValue);
  const followActivityId =
    activity.objectId?.href
    ?? asString(objectValue)
    ?? asString(objectRecord?.id);
  if (actorId == null || followActivityId == null) {
    return null;
  }
  return {
    actor_id: actorId,
    community_actor_id: actorId,
    delivery_id:
      activity.id?.href
      ?? asString(rawRecord?.id)
      ?? fallbackDeliveryId
      ?? `follow-accepted:${followActivityId}`,
    event_type: "follow.accepted",
    object: {
      follow_activity_id: followActivityId,
    },
    occurred_at: new Date().toISOString(),
  };
}

export async function buildLocalFollowRequestedEvent(
  config: GatewayContextData,
  activity: Follow,
): Promise<LocalFollowRequestedEvent | null> {
  const targetActorId = activity.objectId?.href;
  const remoteActorId = activity.actorId?.href;
  if (targetActorId == null || remoteActorId == null || activity.id == null) return null;

  const community = await resolveLocalCommunityByActorUrl(config, targetActorId);
  let targetKind: "community" | "user" | null = community == null ? null : "community";
  if (targetKind == null) {
    const username = parseUserAlias(new URL(targetActorId), config.fedifyOrigin);
    const user = username == null ? null : await loadUserActorIdentity(config, username);
    if (user != null && user.actorId.href === targetActorId) targetKind = "user";
  }
  if (targetKind == null) return null;

  const remoteActor = await activity.getActor({ suppressError: true });
  const remoteInboxUrl = remoteActor?.inboxId?.href;
  if (remoteInboxUrl == null) return null;
  const endpoints = asRecord((remoteActor as unknown as Record<string, unknown>)?.endpoints);
  // Fedify materializes endpoints.sharedInbox as a URL for real dereferenced
  // actor documents, while earlier tests used a plain string fixture. Accept
  // both shapes without trusting any inbox value from the Follow payload.
  const remoteSharedInboxUrl = asUrlString(endpoints?.sharedInbox);

  return {
    actor_id: remoteActorId,
    target_actor_kind: targetKind,
    target_actor_id: targetActorId,
    delivery_id: activity.id.href,
    event_type: "local.follow_requested",
    object: {
      follow_activity_id: activity.id.href,
      remote_inbox_url: remoteInboxUrl,
      remote_shared_inbox_url: remoteSharedInboxUrl,
    },
    occurred_at: new Date().toISOString(),
  };
}

export async function buildLocalUnfollowRequestedEvent(
  config: GatewayContextData,
  activity: Undo,
  rawJson: Record<string, unknown> | null,
): Promise<LocalUnfollowRequestedEvent | null> {
  const undoActorId = activity.actorId?.href ?? asString(rawJson?.actor);
  const embeddedFollow = asRecord(rawJson?.object);
  if (undoActorId == null || embeddedFollow?.type !== "Follow") return null;
  if (asString(embeddedFollow.actor) !== undoActorId) return null;
  const targetActorId = asString(embeddedFollow.object);
  if (targetActorId == null) return null;

  const community = await resolveLocalCommunityByActorUrl(config, targetActorId);
  let targetKind: "community" | "user" | null = community == null ? null : "community";
  if (targetKind == null) {
    const username = parseUserAlias(new URL(targetActorId), config.fedifyOrigin);
    const user = username == null ? null : await loadUserActorIdentity(config, username);
    if (user != null && user.actorId.href === targetActorId) targetKind = "user";
  }
  if (targetKind == null) return null;
  return {
    actor_id: undoActorId,
    target_actor_kind: targetKind,
    target_actor_id: targetActorId,
    delivery_id: activity.id?.href ?? asString(rawJson?.id) ?? `local-unfollow:${Date.now()}`,
    event_type: "local.unfollow_requested",
    object: { follow_activity_id: asString(embeddedFollow.id) },
    occurred_at: new Date().toISOString(),
  };
}

function parseUserAlias(resource: URL, origin: string): string | null {
  // /actors is the canonical registered-user actor namespace. /users remains a
  // compatibility alias so older object/profile links can still resolve.
  const resourceOrigin = new URL(origin);
  if (resource.origin !== resourceOrigin.origin) {
    return null;
  }

  const parts = resource.pathname.split("/").filter(Boolean);
  if (parts.length !== 2 || (parts[0] !== "actors" && parts[0] !== "users")) {
    return null;
  }
  return parts[1];
}
