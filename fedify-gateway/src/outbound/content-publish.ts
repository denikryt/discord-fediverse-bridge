/**
 * User-authored content Create publication for remote and local community flows.
 *
 * The module builds canonical Create(Page|Note) activities and sends remote
 * community publishes through Fedify while preserving the existing unescaped
 * markdown-to-HTML policy for community content.
 */

import { Temporal } from "@js-temporal/polyfill";
import { Create, Note, Page, Source } from "@fedify/vocab";
import type { Federation } from "@fedify/fedify";

import type { GatewayConfig } from "../config.js";
import type { PublishContentRequest, PublishContentResult } from "../types.js";
import { buildGatewayUrl, buildUserActorUri } from "./gateway-url.js";
import { renderPublicActivityJson } from "./activity-json.js";
import { fetchRemoteCommunity } from "./remote-actor.js";

/** Publish one user-authored Create activity to a remote community inbox. */
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
    ctx, request.communityActorUrl,
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
    activityJson: await renderPublicActivityJson(activity),
  };
}

/** Build one canonical user-authored Create(Page|Note) activity. */
export function buildPublishCreateActivity(
  config: GatewayConfig,
  request: PublishContentRequest,
  resolvedCommunityId?: string,
) {
  const actorUri = buildUserActorUri(config, request.actorUsername);
  const objectNumericId = Date.now();
  const objectId = buildGatewayUrl(
    config,
    `/actors/users/${request.actorUsername}/objects/${request.kind === "comment" ? "comments" : "posts"}/${objectNumericId}`,
  );
  const activityId = buildGatewayUrl(
    config,
    `/actors/users/${request.actorUsername}/activities/create/${request.kind === "comment" ? "comments" : "posts"}/${Date.now()}-${Math.random().toString(36).slice(2)}`,
  );
  const communityId = resolvedCommunityId ?? request.communityActorUrl;
  const object = buildPublishObject(request, actorUri, objectId, communityId);
  const PUBLIC = new URL("https://www.w3.org/ns/activitystreams#Public");
  const community = new URL(resolvedCommunityId ?? request.communityActorUrl);
  const followers = buildGatewayUrl(config, `/actors/users/${request.actorUsername}/followers`);
  return {
    actorUri,
    activityId,
    objectId,
    activity: new Create({
      id: activityId,
      actor: actorUri,
      object,
      tos: community == null ? [PUBLIC] : [PUBLIC, community],
      ccs: [actorUri, followers],
    }),
  };
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

/** Convert markdown paragraphs to the existing unescaped community HTML policy. */
export function markdownToHtml(markdown: string): string {
  return markdown
    .split(/\n\n+/)
    .map((p) => `<p>${p.trim().replace(/\n/g, "<br>")}</p>`)
    .join("\n");
}
