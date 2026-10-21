/**
 * Public ActivityPub dereference and collection routes.
 *
 * Route handlers own public request parsing and response selection only. Data
 * reads and actor rendering stay behind narrow ports constructed by the gateway
 * composition root.
 */
import type { Context, Hono } from "hono";

import { buildEmptyOrderedCollection } from "../actors.js";
import { buildPublishedActivityObjectJson, buildPublishedCreateActivityJson } from "../published-objects.js";
import { buildUserOutboxJson, validateCanonicalUserOutboxUrl } from "../user-outbox.js";
import { activityJsonResponse } from "./route-responses.js";

export interface PublicRouteConfig {
  actorIdentifier: string;
  actorName: string;
  actorSummary: string;
  fedifyOrigin: string;
  logLevel: "info" | "debug";
}

export interface LocalCommunityIdentityView {
  slug: string;
}

export interface UserActorIdentityView {
  username: string;
}

export interface PublishedActivityObjectView {
  actorUsername: string;
  actorUrl: string;
  publicationContext: "community" | "personal_feed";
  communityActorUrl: string | null;
  activityId: string;
  objectId: string;
  kind: "post" | "comment";
  title: string | null;
  bodyMarkdown: string;
  inReplyToObjectId: string | null;
  publishedAt: string;
  discordGuildId: number | null;
  discordChannelId: number | null;
  discordMessageId: number | null;
}

export interface UserOutboxPageView {
  outboxUrl: string;
  totalItems: number;
  page: number;
  pageSize: number;
  items: PublishedActivityObjectView[];
}

export interface FollowersPageView {
  followersUrl: string;
  totalItems: number;
  page: number;
  pageSize: number;
  actorIds: string[];
}

export interface PublicActivityPubReadPort {
  loadUserActorIdentity(username: string): Promise<UserActorIdentityView | null>;
  loadLocalCommunityIdentity(slug: string): Promise<LocalCommunityIdentityView | null>;
  hasLocalActor(identifier: string): Promise<boolean>;
  loadPublishedActivityObjectByObjectId(objectUrl: string): Promise<PublishedActivityObjectView | null>;
  loadPublishedActivityObjectByActivityId(activityUrl: string): Promise<PublishedActivityObjectView | null>;
  loadUserOutbox(username: string, page: number, pageSize: number): Promise<UserOutboxPageView | null>;
  loadUserFollowers(username: string, page: number, pageSize: number): Promise<FollowersPageView | null>;
  loadCommunityFollowers(slug: string, page: number, pageSize: number): Promise<FollowersPageView | null>;
}

export interface ActivityPubDocument {
  toJsonLd(): Promise<unknown>;
}

export interface PublicActivityPubRenderPort {
  buildRegisteredUserActorDocument(username: string): Promise<ActivityPubDocument | null>;
  buildLocalCommunityActorDocument(slug: string): Promise<ActivityPubDocument | null>;
}

export interface PublicActivityPubRouteDependencies {
  publicConfig: PublicRouteConfig;
  reads: PublicActivityPubReadPort;
  renders: PublicActivityPubRenderPort;
}

/** Register public ActivityPub routes in the typed typed ActivityPub identity migration namespace. */
export function registerPublicActivityPubRoutes(
  app: Hono,
  dependencies: PublicActivityPubRouteDependencies,
): void {
  // Actor root documents are served by Fedify through /actors/{+identifier}.
  // This module owns actor collections and object/activity dereference routes.
  app.get("/actors/users/:username/outbox", async (context) => {
    return await renderUserOutbox(dependencies, context, context.req.param("username"));
  });

  app.get("/actors/users/:username/followers", async (context) => {
    return await renderUserFollowers(dependencies, context, context.req.param("username"));
  });

  app.get("/actors/users/:username/activities/create/posts/:activityId", async (context) => {
    return await renderCreateActivityForRequest(dependencies, context, context.req.param("username"), "post");
  });

  app.get("/actors/users/:username/activities/create/comments/:activityId", async (context) => {
    return await renderCreateActivityForRequest(dependencies, context, context.req.param("username"), "comment");
  });

  app.get("/actors/users/:username/objects/posts/:objectId", async (context) => {
    return await renderObjectForRequest(dependencies, context, "post");
  });

  app.get("/actors/users/:username/objects/comments/:objectId", async (context) => {
    return await renderObjectForRequest(dependencies, context, "comment");
  });

  app.get("/actors/services/:identifier/outbox", async (context) => {
    const identifier = context.req.param("identifier");
    if (identifier !== dependencies.publicConfig.actorIdentifier) {
      return context.json({ error: "service actor not found" }, 404);
    }
    return activityJsonResponse(
      await buildEmptyOrderedCollection(
        new URL(`/actors/services/${identifier}/outbox`, dependencies.publicConfig.fedifyOrigin),
      ).toJsonLd(),
    );
  });

  app.get("/actors/services/:identifier/followers", async (context) => {
    const identifier = context.req.param("identifier");
    if (identifier !== dependencies.publicConfig.actorIdentifier) {
      return context.json({ error: "service actor not found" }, 404);
    }
    return activityJsonResponse(
      await buildEmptyOrderedCollection(
        new URL(`/actors/services/${identifier}/followers`, dependencies.publicConfig.fedifyOrigin),
      ).toJsonLd(),
    );
  });

  app.get("/actors/communities/:slug/outbox", async (context) => {
    const slug = context.req.param("slug");
    const community = await dependencies.reads.loadLocalCommunityIdentity(slug);
    if (community == null) {
      return context.json({ error: "community actor not found" }, 404);
    }
    return activityJsonResponse(
      await buildEmptyOrderedCollection(
        new URL(`/actors/communities/${slug}/outbox`, dependencies.publicConfig.fedifyOrigin),
      ).toJsonLd(),
    );
  });

  app.get("/actors/communities/:slug/followers", async (context) => {
    const slug = context.req.param("slug");
    const community = await dependencies.reads.loadLocalCommunityIdentity(slug);
    if (community == null) {
      return context.json({ error: "community actor not found" }, 404);
    }
    return await renderFollowersCollection(
      dependencies,
      context,
      () => dependencies.reads.loadCommunityFollowers(slug, parseFollowersPage(context).page, 40),
      "community actor not found",
    );
  });

  app.get("/actors/communities/:slug/objects/posts/:objectId", async (context) => {
    return await renderObjectForRequest(dependencies, context, "post");
  });

  app.get("/actors/communities/:slug/objects/comments/:objectId", async (context) => {
    return await renderObjectForRequest(dependencies, context, "comment");
  });

  app.get("/actors/communities/:slug/activities/create/posts/:activityId", async (context) => {
    return await renderCreateActivityForRequest(dependencies, context, null, "post");
  });

  app.get("/actors/communities/:slug/activities/create/comments/:activityId", async (context) => {
    return await renderCreateActivityForRequest(dependencies, context, null, "comment");
  });

  app.get("/actors/communities/:slug/activities/announce/posts/:activityId", async (context) => {
    return await renderCreateActivityForRequest(dependencies, context, null, "post");
  });

  app.get("/actors/communities/:slug/activities/announce/comments/:activityId", async (context) => {
    return await renderCreateActivityForRequest(dependencies, context, null, "comment");
  });
}

async function renderUserOutbox(
  dependencies: PublicActivityPubRouteDependencies,
  context: Context,
  username: string,
): Promise<Response> {
  const rawPage = context.req.query("page");
  const page = rawPage == null ? 1 : Number(rawPage);
  if (!Number.isInteger(page) || page < 1) {
    return context.json({ error: "invalid page" }, 400);
  }
  let row;
  try {
    row = await dependencies.reads.loadUserOutbox(username, page, 40);
  } catch {
    return context.json({ error: "user outbox unavailable" }, 502);
  }
  if (row == null) {
    return context.json({ error: "user actor not found" }, 404);
  }
  try {
    row = {
      ...row,
      outboxUrl: validateCanonicalUserOutboxUrl(
        row.outboxUrl,
        dependencies.publicConfig.fedifyOrigin,
        username,
      ),
    };
  } catch {
    return context.json({ error: "inconsistent user outbox identity" }, 502);
  }
  return activityJsonResponse(buildUserOutboxJson(row, rawPage == null ? null : page));
}

async function renderUserFollowers(
  dependencies: PublicActivityPubRouteDependencies,
  context: Context,
  username: string,
): Promise<Response> {
  return await renderFollowersCollection(
    dependencies,
    context,
    () => dependencies.reads.loadUserFollowers(username, parseFollowersPage(context).page, 40),
    "user actor not found",
  );
}

function parseFollowersPage(context: Context): { isPageRequest: boolean; page: number; error: Response | null } {
  /** Decode the fixed ActivityStreams followers pagination contract. */
  const pageMarker = context.req.query("page");
  const requestedPageNumber = context.req.query("page_number");
  const isPageRequest = pageMarker != null;
  const page = requestedPageNumber == null ? 1 : Number(requestedPageNumber);
  if (isPageRequest && pageMarker !== "true") {
    return { isPageRequest, page, error: context.json({ error: "invalid pagination" }, 400) };
  }
  if (!Number.isInteger(page) || page < 1 || (!isPageRequest && requestedPageNumber != null)) {
    return { isPageRequest, page, error: context.json({ error: "invalid pagination" }, 400) };
  }
  return { isPageRequest, page, error: null };
}

async function renderFollowersCollection(
  dependencies: PublicActivityPubRouteDependencies,
  context: Context,
  load: () => Promise<FollowersPageView | null>,
  notFoundMessage: string,
): Promise<Response> {
  const pageRequest = parseFollowersPage(context);
  if (pageRequest.error != null) return pageRequest.error;

  const pageSize = 40;
  let data;
  try {
    data = await load();
  } catch {
    // A failed persistence read must never be rendered as an empty social graph.
    return context.json({ error: "followers collection unavailable" }, 502);
  }
  if (data == null) {
    return context.json({ error: notFoundMessage }, 404);
  }

  const id = data.followersUrl;
  if (!pageRequest.isPageRequest) {
    return activityJsonResponse({
      "@context": "https://www.w3.org/ns/activitystreams",
      type: "OrderedCollection",
      id,
      totalItems: data.totalItems,
      ...(data.totalItems > 0 ? { first: `${id}?page=true` } : {}),
    });
  }

  const totalPages = Math.ceil(data.totalItems / pageSize);
  if (pageRequest.page > totalPages || totalPages === 0) {
    return context.json({ error: "followers page not found" }, 404);
  }
  const pageId = pageRequest.page === 1 ? `${id}?page=true` : `${id}?page=true&page_number=${pageRequest.page}`;
  return activityJsonResponse({
    "@context": "https://www.w3.org/ns/activitystreams",
    type: "OrderedCollectionPage",
    id: pageId,
    partOf: id,
    totalItems: data.totalItems,
    orderedItems: data.actorIds,
    ...(pageRequest.page > 1 ? { prev: `${id}?page=true&page_number=${pageRequest.page - 1}` } : {}),
    ...(pageRequest.page < totalPages ? { next: `${id}?page=true&page_number=${pageRequest.page + 1}` } : {}),
  });
}

async function renderObjectForRequest(
  dependencies: PublicActivityPubRouteDependencies,
  context: Context,
  kind: "post" | "comment",
): Promise<Response> {
  const object = await loadPublishedObjectForRequest(dependencies, context.req.path);
  if (object == null || object.kind !== kind) {
    return context.json({ error: `published ${kind} not found` }, 404);
  }
  return activityJsonResponse(buildPublishedActivityObjectJson(object));
}

async function renderCreateActivityForRequest(
  dependencies: PublicActivityPubRouteDependencies,
  context: Context,
  username: string | null,
  kind: "post" | "comment",
): Promise<Response> {
  const activity = await loadPublishedCreateActivityForRequest(
    dependencies,
    context.req.path,
    username,
    kind,
  );
  if (activity == null) {
    return context.json({ error: "published create activity not found" }, 404);
  }
  return activityJsonResponse(buildPublishedCreateActivityJson(activity));
}

async function loadPublishedObjectForRequest(
  dependencies: PublicActivityPubRouteDependencies,
  requestPath: string,
): Promise<PublishedActivityObjectView | null> {
  // Route lookup uses the exact canonical object URL, which keeps restart
  // behavior deterministic and avoids rebuilding object IDs from fragments.
  const objectUrl = new URL(requestPath, dependencies.publicConfig.fedifyOrigin).href;
  return await dependencies.reads.loadPublishedActivityObjectByObjectId(objectUrl);
}

async function loadPublishedCreateActivityForRequest(
  dependencies: PublicActivityPubRouteDependencies,
  requestPath: string,
  username: string | null,
  kind: "post" | "comment",
): Promise<PublishedActivityObjectView | null> {
  // The delivered Create.id is a full URL. Reconstruct it from the request path
  // so dereference uses the exact same durable id Python stored after publish.
  const activityUrl = new URL(requestPath, dependencies.publicConfig.fedifyOrigin).href;
  const activity = await dependencies.reads.loadPublishedActivityObjectByActivityId(activityUrl);
  if (activity == null || activity.kind !== kind) {
    return null;
  }
  if (username != null && activity.actorUsername !== username) {
    return null;
  }
  return activity;
}
