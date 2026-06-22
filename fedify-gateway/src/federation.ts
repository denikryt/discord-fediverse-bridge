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
import { admitRemoteDeliveryEndpoint, type RemoteEndpointAdmissionOptions } from "./remote-delivery-endpoints.js";
import { parseTypedActorIdentifier } from "./typed-actor-routing.js";
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
    skipSignatureVerification: config.skipSignatureVerificationForTests === true,
  });

  federation
    .setActorDispatcher("/actors/{+identifier}", async (ctx, identifier) => {
      const sharedInboxId = ctx.getInboxUri();
      const typed = parseTypedActorIdentifier(identifier);
      if (typed == null) return null;
      const actorKind = await resolveLocalActorKind(config, identifier);

      if (actorKind === "service") {
        const bridgeIdentity = getBridgeActorIdentity(config);
        const bridgeKeys = await ctx.getActorKeyPairs(identifier);
        return buildBridgeServiceActor(
          bridgeIdentity,
          sharedInboxId,
          bridgeKeys,
        );
      }

      if (actorKind === "user") {
        const userIdentity = await loadUserActorIdentity(config, typed.identifier);
        if (userIdentity == null) {
          return null;
        }

        // The typed tail must be used for key-pair lookup because Fedify binds
        // key ownership to the dispatcher identifier it received.
        return buildUserPersonActor(
          userIdentity,
          sharedInboxId,
          await ctx.getActorKeyPairs(identifier),
        );
      }

      if (actorKind === "community") {
        const communityIdentity = await loadLocalCommunityIdentity(config, typed.identifier);
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
      if (username === config.actorIdentifier) {
        return `services/${config.actorIdentifier}`;
      }
      if ((await loadUserActorIdentity(config, username)) != null) {
        return `users/${username}`;
      }
      if ((await loadLocalCommunityIdentity(config, username)) != null) {
        return `communities/${username}`;
      }
      return null;
    })
    .mapAlias(async (_ctx, resource) => {
      const tail = parseTypedActorAlias(resource, config.fedifyOrigin);
      if (tail == null || !(await hasLocalActor(config, tail))) {
        return null;
      }
      return { identifier: tail };
    });

  federation
    .setInboxListeners("/actors/{+identifier}/inbox", "/inbox")
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
      const activityId = activity.id?.href;
      const cachedRaw = activityId == null ? null : getRawActivity(activityId);
      const rawForUndo = asRecord(cachedRaw?.rawJson ?? ctx.data.activitypubRawJson);
      const event = await buildLocalUnfollowRequestedEvent(
        config,
        activity,
        rawForUndo,
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
  endpointAdmission: RemoteEndpointAdmissionOptions = {},
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
  const dereferencedActorId = asUrlString(
    (remoteActor as unknown as Record<string, unknown> | null)?.id,
  );
  const remoteInboxUrl = remoteActor?.inboxId?.href;
  if (remoteInboxUrl == null) return null;
  const endpoints = asRecord((remoteActor as unknown as Record<string, unknown>)?.endpoints);
  // Fedify materializes endpoints.sharedInbox as a URL for real dereferenced
  // actor documents, while earlier tests used a plain string fixture. Accept
  // both shapes without trusting any inbox value from the Follow payload.
  const remoteSharedInboxUrl = asUrlString(endpoints?.sharedInbox);
  let admittedInboxUrl = remoteInboxUrl;
  let admittedSharedInboxUrl = remoteSharedInboxUrl;
  if (targetKind === "user") {
    if (dereferencedActorId !== remoteActorId) {
      // The same remote principal must own the activity, actor document, inbox,
      // persisted follower identity, and later delivery endpoint.  Stage 5
      // enforces this for User Actor follows without changing Stage 6 community
      // semantics early.
      return null;
    }
    try {
      admittedInboxUrl = admitRemoteDeliveryEndpoint(remoteInboxUrl, {
        ...endpointAdmission,
        localOrigin: config.fedifyOrigin,
      });
      admittedSharedInboxUrl = remoteSharedInboxUrl == null
        ? null
        : admitRemoteDeliveryEndpoint(remoteSharedInboxUrl, {
          ...endpointAdmission,
          localOrigin: config.fedifyOrigin,
        });
    } catch {
      return null;
    }
  }

  return {
    actor_id: remoteActorId,
    target_actor_kind: targetKind,
    target_actor_id: targetActorId,
    delivery_id: activity.id.href,
    event_type: "local.follow_requested",
    object: {
      follow_activity_id: activity.id.href,
      remote_inbox_url: admittedInboxUrl,
      remote_shared_inbox_url: admittedSharedInboxUrl,
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

function parseTypedActorAlias(resource: URL, origin: string): string | null {
  // Only typed actor aliases are accepted after Stage 4; old actor URLs are not
  // rewritten because they are no longer a compatibility surface.
  if (resource.origin !== new URL(origin).origin) return null;
  const match = resource.pathname.match(/^\/actors\/(services|users|communities)\/([^/]+)$/);
  return match == null ? null : `${match[1]}/${match[2]}`;
}

function parseUserAlias(resource: URL, origin: string): string | null {
  // Stage 4 accepts only the typed user actor URL as local User identity.
  const resourceOrigin = new URL(origin);
  if (resource.origin !== resourceOrigin.origin) {
    return null;
  }

  const parts = resource.pathname.split("/").filter(Boolean);
  if (parts.length !== 3 || parts[0] !== "actors" || parts[1] !== "users") {
    return null;
  }
  return parts[2];
}
