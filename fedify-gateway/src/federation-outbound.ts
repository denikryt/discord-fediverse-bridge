import { Temporal } from "@js-temporal/polyfill";
import { Create, Delete, Follow, Note, Page, Source, Undo, Update } from "@fedify/vocab";
import type { Federation } from "@fedify/fedify";
import type { GatewayConfig } from "./config.js";
import type {
  DeleteContentRequest,
  PublishContentRequest,
  PublishContentResult,
  UpdateContentRequest,
} from "./types.js";

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

  console.log("[Follow] Sending Follow activity:", {
    actorUri: actorUri.toString(),
    communityId,
    inboxUrl,
    followId: follow.id?.toString(),
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

  const undo = new Undo({
    id: new URL(
      `${config.fedifyOrigin}activities/undo/${Date.now()}/${Math.random().toString(36).slice(2)}`,
    ),
    actor: actorUri,
    object: new Follow({
      id: new URL(followActivityId),
      actor: actorUri,
      object: new URL(communityId),
    }),
  });

  console.log("[Unfollow] Sending Undo(Follow) activity:", {
    actorUri: actorUri.toString(),
    communityId,
    inboxUrl,
    followActivityId,
    undoId: undo.id?.toString(),
  });

  await ctx.sendActivity(
    { username: config.actorIdentifier },
    { id: new URL(communityId), inboxId: new URL(inboxUrl) },
    undo,
  );

  console.log("[Unfollow] Successfully sent Undo(Follow) activity");
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
  return new URL(`/users/${actorUsername}`, config.fedifyOrigin);
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
