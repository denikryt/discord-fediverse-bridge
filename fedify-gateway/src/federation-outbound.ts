import { createHash, webcrypto } from "node:crypto";

const ACTIVITYSTREAMS_PUBLIC_IRI = "https://www.w3.org/ns/activitystreams#Public";

import { Temporal } from "@js-temporal/polyfill";
import { Accept, Announce, Create, Delete, Follow, Note, Page, Source, Undo, Update } from "@fedify/vocab";
import type { Federation } from "@fedify/fedify";
import type { GatewayConfig } from "./config.js";
import { appendDebugFileLog } from "./debug-file-log.js";
import { loadActorKeyPair } from "./actor-store.js";
import { loadAcceptedLocalCommunityFollowersByActorUrl } from "./db.js";
import {
  loadLocalCommunitySigningKey,
  loadLocalCommunitySigningKeyByActorUrl,
} from "./local-community-keys.js";
import type {
  AcceptLocalCommunityFollowRequest,
  DeleteContentRequest,
  PublishContentRequest,
  PublishContentResult,
  PublishLocalCommunityContentRequest,
  PublishLocalCommunityContentResult,
  SendLocalCommunityRelayRequest,
  SendLocalCommunityRelayResult,
  UpdateContentRequest,
} from "./types.js";


function getUnfollowObjectMode(): "embedded" | "iri" {
  const rawMode = process.env.UNFOLLOW_OBJECT_MODE?.trim().toLowerCase();
  if (rawMode == null || rawMode === "" || rawMode === "embedded") {
    return "embedded";
  }
  if (rawMode === "iri") {
    return "iri";
  }
  throw new Error(
    `Invalid UNFOLLOW_OBJECT_MODE=${process.env.UNFOLLOW_OBJECT_MODE}; expected embedded or iri`,
  );
}

export interface FollowCommunityResult {
  communityActorUrl: string;
  communityInboxUrl: string;
  followActivityId: string;
}

export async function followCommunity(
  federation: Federation<GatewayConfig>,
  config: GatewayConfig,
  communityActorUrl: string,
): Promise<FollowCommunityResult> {
  // Outbound follow is kept explicit and separate from inbound delivery so the
  // gateway can be pointed at a known community actor before bot commands exist.
  const ctx = federation.createContext(new URL(config.fedifyOrigin), config);
  const actorUri = ctx.getActorUri(config.actorIdentifier);

  // Fetch the remote actor first because Follow delivery needs both the actor
  // id and its concrete inbox endpoint.
  const communityResponse = await fetch(communityActorUrl, {
    headers: {
      Accept: "application/activity+json",
    },
  });
  if (!communityResponse.ok) {
    throw new Error(
      `Failed to fetch community actor: ${communityResponse.status}`,
    );
  }
  const communityActor = await communityResponse.json();
  const inboxUrl = communityActor.inbox;
  const communityId = communityActor.id;

  if (!inboxUrl) {
    throw new Error("Community actor does not have an inbox");
  }

  if (!communityId) {
    throw new Error("Community actor does not have an id");
  }

  // Build a unique Follow id locally so retries and remote logs can refer to a
  // concrete outbound activity URL.
  const follow = new Follow({
    id: new URL(
      `${config.fedifyOrigin}activities/follow/${Date.now()}/${Math.random().toString(36).slice(2)}`,
    ),
    actor: actorUri,
    object: new URL(communityId),
  });

  const followJson = await follow.toJsonLd();
  console.log("[Follow] Sending Follow activity:", {
    actorUri: actorUri.toString(),
    communityId,
    inboxUrl,
    followId: follow.id?.toString(),
  });
  appendDebugFileLog("follow.outbound", {
    actorUri: actorUri.toString(),
    communityActorUrl,
    communityId,
    inboxUrl,
    followActivityId: follow.id?.toString() ?? null,
    activity: followJson as Record<string, unknown>,
  });

  // Fedify signs and delivers the activity; this helper only prepares the
  // target identity and object URLs.
  await ctx.sendActivity(
    { username: config.actorIdentifier },
    { id: new URL(communityId), inboxId: new URL(inboxUrl) },
    follow,
  );

  console.log("[Follow] Successfully sent Follow activity");
  return {
    communityActorUrl: communityId,
    communityInboxUrl: inboxUrl,
    followActivityId: follow.id?.href ?? follow.id?.toString() ?? "",
  };
}

export async function unfollowCommunity(
  federation: Federation<GatewayConfig>,
  config: GatewayConfig,
  communityActorUrl: string,
  followActivityId: string,
): Promise<void> {
  const ctx = federation.createContext(new URL(config.fedifyOrigin), config);
  const actorUri = ctx.getActorUri(config.actorIdentifier);

  const communityResponse = await fetch(communityActorUrl, {
    headers: { Accept: "application/activity+json" },
  });
  if (!communityResponse.ok) {
    throw new Error(`Failed to fetch community actor: ${communityResponse.status}`);
  }
  const communityActor = await communityResponse.json();
  const inboxUrl = communityActor.inbox;
  const communityId = communityActor.id;

  if (!inboxUrl) throw new Error("Community actor does not have an inbox");
  if (!communityId) throw new Error("Community actor does not have an id");

  const unfollowObjectMode = getUnfollowObjectMode();
  const communityActorId = new URL(communityId);
  const undoObject = unfollowObjectMode === "iri"
    ? new URL(followActivityId)
    : new Follow({
        id: new URL(followActivityId),
        actor: actorUri,
        object: communityActorId,
        tos: [communityActorId],
      });

  const undo = new Undo({
    id: new URL(
      `${config.fedifyOrigin}activities/undo/${Date.now()}/${Math.random().toString(36).slice(2)}`,
    ),
    actor: actorUri,
    object: undoObject,
    tos: [communityActorId],
  });

  const undoJson = await renderPublicActivityJson(undo);
  console.log("[Unfollow] Sending Undo(Follow) activity:", {
    actorUri: actorUri.toString(),
    communityId,
    inboxUrl,
    followActivityId,
    undoId: undo.id?.toString(),
    objectMode: unfollowObjectMode,
    deliveryBackend: "signed-json",
  });
  appendDebugFileLog("unfollow.outbound", {
    actorUri: actorUri.toString(),
    communityActorUrl,
    communityId,
    inboxUrl,
    followActivityId,
    undoActivityId: undo.id?.toString() ?? null,
    objectMode: unfollowObjectMode,
    deliveryBackend: "signed-json",
    activity: undoJson,
  });

  const keyPair = await loadActorKeyPair(config, config.actorIdentifier);
  if (keyPair == null) {
    throw new Error("Bridge actor signing key not found");
  }
  await sendSignedJsonActivity(
    "Unfollow",
    inboxUrl,
    { keyId: new URL("#main-key", actorUri), privateKey: keyPair.privateKey },
    undoJson,
    { debugDelivery: shouldLogSignedJsonDelivery(config) },
  );

  console.log("[Unfollow] Successfully sent Undo(Follow) activity", {
    deliveryBackend: "signed-json",
  });
}

export async function publishContent(
  federation: Federation<GatewayConfig>,
  config: GatewayConfig,
  request: PublishContentRequest,
): Promise<PublishContentResult> {
  // Publish delivery mirrors follow delivery: Python decides policy, while the
  // gateway owns the signed ActivityPub delivery from the local actor.
  const ctx = federation.createContext(new URL(config.fedifyOrigin), config);
  const actorUri = buildUserActorUri(config, request.actorUsername);
  const { communityId, inboxUrl } = await fetchRemoteCommunity(
    request.communityActorUrl,
  );
  const builtCreate = buildPublishCreateActivity(config, request, communityId);
  const activity = builtCreate.activity;
  const objectId = builtCreate.objectId;
  const activityId = builtCreate.activityId;

  console.log("[Publish] Sending user-authored Create activity:", {
    actorUsername: request.actorUsername,
    actorUri: actorUri.toString(),
    kind: request.kind,
    communityId,
    inboxUrl,
    activityId: activityId.toString(),
    objectId: objectId.toString(),
  });

  try {
    await ctx.sendActivity(
      { username: request.actorUsername },
      { id: new URL(communityId), inboxId: new URL(inboxUrl) },
      activity,
    );
    console.log("[Publish] sendActivity completed successfully");
  } catch (err) {
    console.error("[Publish] sendActivity failed:", err);
    throw err;
  }

  return {
    activityId: activityId.href,
    objectId: objectId.href,
    communityActorUrl: communityId,
  };
}

export async function publishLocalCommunityContent(
  federation: Federation<GatewayConfig>,
  config: GatewayConfig,
  request: PublishLocalCommunityContentRequest,
): Promise<PublishLocalCommunityContentResult> {
  // Local-community fanout is performed by the bridge-owned community actor.
  // The embedded Create remains user-authored, while the outer Announce makes
  // delivery look like Lemmy Group fanout to Mastodon and other followers.
  const followers = await loadAcceptedLocalCommunityFollowersByActorUrl(
    config,
    request.communityActorUrl,
  );
  if (followers.length === 0) {
    throw new Error("Local community has no accepted followers");
  }

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
  const sender = [{ keyId: signingKey.keyId, privateKey: signingKey.privateKey }];

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

  let deliveredFollowerCount = 0;
  let failedFollowerCount = 0;

  const debugDelivery = shouldLogSignedJsonDelivery(config);

  for (const follower of followers) {
    try {
      await sendSignedJsonActivity(
        "LocalCommunityPublish",
        follower.remoteInboxUrl,
        signingKey,
        activityJson,
        { debugDelivery },
      );
      console.log("[LocalCommunityPublish] delivery completed:", {
        remoteActorId: follower.remoteActorId,
        remoteInboxUrl: follower.remoteInboxUrl,
        signingKeyId: signingKey.keyId.href,
        deliveryBackend: "signed-json",
        debugDelivery,
      });
      deliveredFollowerCount += 1;
    } catch (error) {
      // Fanout must continue toward healthy followers even if one target
      // rejects the activity or is temporarily unreachable.
      failedFollowerCount += 1;
      console.error("[LocalCommunityPublish] signed JSON delivery failed:", {
        remoteActorId: follower.remoteActorId,
        remoteInboxUrl: follower.remoteInboxUrl,
        signingKeyId: signingKey.keyId.href,
        deliveryBackend: "signed-json",
        debugDelivery,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }

  if (deliveredFollowerCount === 0) {
    throw new Error("Local community publish failed for all accepted followers");
  }

  return {
    activityId: activityId.href,
    objectId: objectId.href,
    communityActorUrl: request.communityActorUrl,
    deliveredFollowerCount,
    failedFollowerCount,
  };
}


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


export async function acceptLocalCommunityFollow(
  federation: Federation<GatewayConfig>,
  config: GatewayConfig,
  request: AcceptLocalCommunityFollowRequest,
): Promise<void> {
  // Python persists local-community followers first, then asks the gateway to
  // sign and deliver the Accept from the community actor.
  const ctx = federation.createContext(new URL(config.fedifyOrigin), config);
  const communityActorUri = new URL(request.communityActorUrl);
  const remoteActorUri = new URL(request.remoteActorId);
  const signingKey = await loadLocalCommunitySigningKey(config, request.communitySlug);
  if (signingKey == null) {
    throw new Error("Local community signing key not found");
  }
  if (signingKey.actorId.href !== communityActorUri.href) {
    throw new Error("communityActorUrl must match the canonical local community actor URL");
  }
  const sender = [{ keyId: signingKey.keyId, privateKey: signingKey.privateKey }];
  const acceptId = new URL(
    `${config.fedifyOrigin}communities/${request.communitySlug}/activities/accept/${Date.now()}-${Math.random().toString(36).slice(2)}`,
  );

  const accept = new Accept({
    id: acceptId,
    actor: communityActorUri,
    to: remoteActorUri,
    object: new Follow({
      id: new URL(request.followActivityId),
      actor: remoteActorUri,
      object: communityActorUri,
    }),
  });

  console.log("[LocalCommunityFollow] Sending Accept(Follow):", {
    communitySlug: request.communitySlug,
    communityActorUrl: request.communityActorUrl,
    remoteActorId: request.remoteActorId,
    remoteInboxUrl: request.remoteInboxUrl,
    followActivityId: request.followActivityId,
    acceptId: acceptId.href,
    signingKeyId: signingKey.keyId.href,
  });
  try {
    await ctx.sendActivity(
      sender,
      { id: remoteActorUri, inboxId: new URL(request.remoteInboxUrl) },
      accept,
    );
    console.log("[LocalCommunityFollow] Accept(Follow) delivered:", {
      communitySlug: request.communitySlug,
      remoteActorId: request.remoteActorId,
      followActivityId: request.followActivityId,
      acceptId: acceptId.href,
      signingKeyId: signingKey.keyId.href,
    });
  } catch (error) {
    console.error("[LocalCommunityFollow] Accept(Follow) failed:", {
      communitySlug: request.communitySlug,
      remoteActorId: request.remoteActorId,
      remoteInboxUrl: request.remoteInboxUrl,
      followActivityId: request.followActivityId,
      signingKeyId: signingKey.keyId.href,
      error: error instanceof Error ? error.message : String(error),
    });
    throw error;
  }
}

interface SignedJsonDeliveryOptions {
  debugDelivery: boolean;
}

async function sendSignedJsonActivity(
  label: string,
  inboxUrl: string,
  signingKey: { keyId: URL; privateKey: CryptoKey },
  activityJson: Record<string, unknown>,
  options: SignedJsonDeliveryOptions,
): Promise<void> {
  // Local-community fanout uses an exact JSON wire contract that mirrors
  // Lemmy's Announce(Create(...)) shape. Fedify's high-level sendActivity
  // expects Fedify-native activity objects, so this path signs and posts the
  // already-rendered JSON directly while keeping detailed body logs opt-in.
  const inbox = new URL(inboxUrl);
  const body = JSON.stringify(activityJson);
  const digest = `SHA-256=${createHash("sha256").update(body).digest("base64")}`;
  const date = new Date().toUTCString();
  const contentType = "application/activity+json";
  const requestTarget = `post ${inbox.pathname}${inbox.search}`;
  const signingString = [
    `(request-target): ${requestTarget}`,
    `host: ${inbox.host}`,
    `date: ${date}`,
    `digest: ${digest}`,
    `content-type: ${contentType}`,
  ].join("\n");
  const signatureBytes = await webcrypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    signingKey.privateKey,
    Buffer.from(signingString),
  );
  const signature = Buffer.from(signatureBytes).toString("base64");
  const signatureHeader = [
    `keyId="${signingKey.keyId.href}"`,
    `algorithm="rsa-sha256"`,
    `headers="(request-target) host date digest content-type"`,
    `signature="${signature}"`,
  ].join(",");
  const headers = {
    Host: inbox.host,
    Date: date,
    Digest: digest,
    "Content-Type": contentType,
    Signature: signatureHeader,
  };

  if (options.debugDelivery) {
    console.log(`[${label}] Raw ActivityPub request:`, {
      inboxUrl,
      body,
      headers,
      signingString,
    });
  }
  const response = await fetch(inbox, { method: "POST", headers, body });
  const responseBody = await response.text();
  if (options.debugDelivery) {
    console.log(`[${label}] Raw ActivityPub response:`, {
      inboxUrl,
      status: response.status,
      statusText: response.statusText,
      responseBody,
    });
  }
  if (!response.ok) {
    throw new Error(
      `${label} signed JSON delivery failed with HTTP ${response.status}: ${responseBody}`,
    );
  }
}

function shouldLogSignedJsonDelivery(config: GatewayConfig): boolean {
  // LOG_LEVEL=debug is the single operator switch for verbose gateway
  // diagnostics, including signed JSON delivery request/response logging.
  return config.logLevel === "debug";
}

type ActivityJsonValue = Record<string, unknown> | unknown[] | string | number | boolean | null;

async function renderPublicActivityJson(activity: { toJsonLd(): Promise<unknown> }): Promise<Record<string, unknown>> {
  // Fedify's JSON-LD serializer may compact the public collection as
  // `as:Public`. Lemmy's local-community validation rejected that compact IRI
  // as `object_is_not_public`, so local-community fanout expands it before
  // signing or sending the activity.
  return normalizeActivityPubJson(await activity.toJsonLd() as ActivityJsonValue) as Record<string, unknown>;
}

function normalizeActivityPubJson(value: ActivityJsonValue, key?: string): ActivityJsonValue {
  // ActivityPub addressing fields are scalar-or-array in the vocabulary, but
  // using arrays consistently keeps the wire shape close to Lemmy's own
  // Announce(Create(...)) activities and avoids target-specific ambiguity.
  if (typeof value === "string") {
    const expanded = value === "as:Public" ? ACTIVITYSTREAMS_PUBLIC_IRI : value;
    if ((key === "to" || key === "cc" || key === "bto" || key === "bcc") && expanded !== value) {
      return [expanded];
    }
    return expanded;
  }

  if (Array.isArray(value)) {
    return value.map((item) => normalizeActivityPubJson(item as ActivityJsonValue));
  }

  if (value != null && typeof value === "object") {
    const normalized: Record<string, unknown> = {};
    for (const [entryKey, entryValue] of Object.entries(value)) {
      const nextValue = normalizeActivityPubJson(entryValue as ActivityJsonValue, entryKey);
      if ((entryKey === "to" || entryKey === "cc" || entryKey === "bto" || entryKey === "bcc") && nextValue != null && !Array.isArray(nextValue)) {
        normalized[entryKey] = [nextValue];
      } else {
        normalized[entryKey] = nextValue;
      }
    }
    return normalized;
  }

  return value;
}

export function buildLocalCommunityAnnounceActivity(
  config: GatewayConfig,
  communityActorUrl: string,
  createActivity: Create,
): Announce {
  const communityActor = new URL(communityActorUrl);
  const PUBLIC = new URL("https://www.w3.org/ns/activitystreams#Public");
  const announceId = new URL(
    `/communities/${encodeURIComponent(communityActor.pathname.split("/").filter(Boolean).at(-1) ?? "community")}/activities/announce/${Date.now()}-${Math.random().toString(36).slice(2)}`,
    config.fedifyOrigin,
  );

  // The Announce is only a delivery envelope. User authorship and canonical
  // object identity stay in the embedded Create(Page|Note), which keeps Lemmy
  // author display and Python's Discord mapping ids stable.
  return new Announce({
    id: announceId,
    actor: communityActor,
    object: createActivity,
    tos: [PUBLIC],
    ccs: [new URL("followers", `${communityActor.href.replace(/\/$/, "")}/`)],
  });
}


export function buildPublishCreateActivity(
  config: GatewayConfig,
  request: PublishContentRequest,
  resolvedCommunityId?: string,
) {
  const actorUri = buildUserActorUri(config, request.actorUsername);
  const objectNumericId = Date.now();
  const objectId = new URL(
    `/users/${request.actorUsername}/${request.kind}/${objectNumericId}`,
    config.fedifyOrigin,
  );
  const activityId = new URL(
    `/users/${request.actorUsername}/activities/create/${request.kind}/${Date.now()}-${Math.random().toString(36).slice(2)}`,
    config.fedifyOrigin,
  );
  const communityId = resolvedCommunityId ?? request.communityActorUrl;
  const object = buildPublishObject(request, actorUri, objectId, communityId);
  const PUBLIC = new URL("https://www.w3.org/ns/activitystreams#Public");
  const community = new URL(resolvedCommunityId ?? request.communityActorUrl);
  return {
    actorUri,
    activityId,
    objectId,
    activity: new Create({
      id: activityId,
      actor: actorUri,
      object,
      tos: [PUBLIC, community],
      ccs: [actorUri],
    }),
  };
}

function buildUserActorUri(config: GatewayConfig, actorUsername: string): URL {
  // Registered users are canonical Fedify actors. Their posts/comments can keep
  // /users object URLs, but author identity and signing use /actors.
  return new URL(`/actors/${actorUsername}`, config.fedifyOrigin);
}

function buildPublishObject(
  request: PublishContentRequest,
  actorUri: URL,
  objectId: URL,
  communityId: string,
) {
  const source = new Source({
    content: request.bodyMarkdown,
    mediaType: "text/markdown",
  });

  const PUBLIC = new URL("https://www.w3.org/ns/activitystreams#Public");
  const community = new URL(communityId);
  const published = Temporal.Now.instant();
  const htmlContent = markdownToHtml(request.bodyMarkdown);

  if (request.kind === "post") {
    return new Page({
      id: objectId,
      name: request.title ?? "Untitled Discord Post",
      attribution: actorUri,
      audience: community,
      tos: [PUBLIC, community],
      ccs: [actorUri],
      source,
      content: htmlContent,
      published,
      url: objectId,
    });
  }

  if (request.inReplyToObjectId == null) {
    throw new Error("Comment publish requires inReplyToObjectId");
  }
  return new Note({
    id: objectId,
    attribution: actorUri,
    audience: community,
    tos: [PUBLIC, community],
    ccs: [actorUri],
    source,
    content: htmlContent,
    published,
    replyTarget: new URL(request.inReplyToObjectId),
    url: objectId,
  });
}

export async function updateContent(
  federation: Federation<GatewayConfig>,
  config: GatewayConfig,
  request: UpdateContentRequest,
): Promise<void> {
  // Update delivery mirrors publish delivery: same actor URI, same community
  // fetch, but wraps the object in an Update activity instead of Create.
  const ctx = federation.createContext(new URL(config.fedifyOrigin), config);
  const actorUri = buildUserActorUri(config, request.actorUsername);
  const { communityId, inboxUrl } = await fetchRemoteCommunity(
    request.communityActorUrl,
  );

  const objectId = new URL(request.apObjectId);
  const activityId = new URL(
    `/users/${request.actorUsername}/activities/update/${request.kind}/${Date.now()}-${Math.random().toString(36).slice(2)}`,
    config.fedifyOrigin,
  );
  const htmlContent = markdownToHtml(request.bodyMarkdown);
  const source = new Source({
    content: request.bodyMarkdown,
    mediaType: "text/markdown",
  });
  const PUBLIC = new URL("https://www.w3.org/ns/activitystreams#Public");
  const community = new URL(communityId);
  const updated = Temporal.Now.instant();

  // Build the full updated object — Lemmy requires all original fields plus the
  // updated timestamp and new content. The actor must match the original attributedTo.
  let object;
  if (request.kind === "post") {
    object = new Page({
      id: objectId,
      name: request.title ?? "Untitled Discord Post",
      attribution: actorUri,
      audience: community,
      tos: [PUBLIC, community],
      ccs: [actorUri],
      source,
      content: htmlContent,
      updated,
      url: objectId,
    });
  } else {
    const noteParams: ConstructorParameters<typeof Note>[0] = {
      id: objectId,
      attribution: actorUri,
      audience: community,
      tos: [PUBLIC, community],
      ccs: [actorUri],
      source,
      content: htmlContent,
      updated,
    };
    // For comments, inReplyTo is required by Lemmy to identify the parent post
    // Lemmy expects inReplyTo as an array of URLs
    if (request.inReplyToObjectId) {
      noteParams.replyTarget = new URL(request.inReplyToObjectId);
    }
    object = new Note(noteParams);
  }

  const activity = new Update({
    id: activityId,
    actor: actorUri,
    object,
    tos: [PUBLIC, community],
    ccs: [actorUri],
  });

  console.log("[Update] Sending Update activity:", {
    actorUsername: request.actorUsername,
    kind: request.kind,
    apObjectId: request.apObjectId,
    inReplyToObjectId: request.inReplyToObjectId,
    bodyMarkdown: request.bodyMarkdown,
    communityId,
    activityId: activityId.toString(),
  });

  try {
    const activityJson = await activity.toJsonLd();
    console.log("[Update] Activity JSON-LD (before sendActivity):", JSON.stringify(activityJson, null, 2));

    await ctx.sendActivity(
      { username: request.actorUsername },
      { id: new URL(communityId), inboxId: new URL(inboxUrl) },
      activity,
    );
    console.log("[Update] sendActivity completed successfully");
  } catch (err) {
    console.error("[Update] sendActivity failed:", err);
    throw err;
  }
}

export async function deleteContent(
  federation: Federation<GatewayConfig>,
  config: GatewayConfig,
  request: DeleteContentRequest,
): Promise<void> {
  // Delete delivery uses the AP object URL as the object field (string form, not
  // a full object), matching the Lemmy federation protocol for Delete activities.
  const ctx = federation.createContext(new URL(config.fedifyOrigin), config);
  const actorUri = buildUserActorUri(config, request.actorUsername);
  const { communityId, inboxUrl } = await fetchRemoteCommunity(
    request.communityActorUrl,
  );

  const activityId = new URL(
    `/users/${request.actorUsername}/activities/delete/${Date.now()}-${Math.random().toString(36).slice(2)}`,
    config.fedifyOrigin,
  );
  const PUBLIC = new URL("https://www.w3.org/ns/activitystreams#Public");
  const community = new URL(communityId);

  const activity = new Delete({
    id: activityId,
    actor: actorUri,
    // Lemmy Delete uses the object URL as a plain URL, not a full object body.
    object: new URL(request.apObjectId),
    tos: [PUBLIC, community],
    ccs: [actorUri],
  });

  console.log("[Delete] Sending Delete activity:", {
    actorUsername: request.actorUsername,
    apObjectId: request.apObjectId,
    communityId,
    activityId: activityId.toString(),
  });

  try {
    await ctx.sendActivity(
      { username: request.actorUsername },
      { id: new URL(communityId), inboxId: new URL(inboxUrl) },
      activity,
    );
    console.log("[Delete] sendActivity completed successfully");
  } catch (err) {
    console.error("[Delete] sendActivity failed:", err);
    throw err;
  }
}

function markdownToHtml(markdown: string): string {
  return markdown
    .split(/\n\n+/)
    .map((p) => `<p>${p.trim().replace(/\n/g, "<br>")}</p>`)
    .join("\n");
}

async function fetchRemoteCommunity(
  communityActorUrl: string,
): Promise<{ communityId: string; inboxUrl: string }> {
  // Both follow and publish need the remote actor's canonical id and inbox, so
  // the fetch/validation logic is shared here instead of being repeated.
  const communityResponse = await fetch(communityActorUrl, {
    headers: {
      Accept: "application/activity+json",
    },
  });
  if (!communityResponse.ok) {
    throw new Error(
      `Failed to fetch community actor: ${communityResponse.status}`,
    );
  }
  const communityActor = await communityResponse.json();
  const inboxUrl = communityActor.inbox;
  const communityId = communityActor.id;

  if (!inboxUrl) {
    throw new Error("Community actor does not have an inbox");
  }
  if (!communityId) {
    throw new Error("Community actor does not have an id");
  }

  return { communityId, inboxUrl };
}
