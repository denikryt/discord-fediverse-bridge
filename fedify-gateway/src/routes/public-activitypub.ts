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

export interface UserFollowersPageView {
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
  loadUserFollowers(username: string, page: number, pageSize: number): Promise<UserFollowersPageView | null>;
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

/** Register public ActivityPub routes in the same order as the former server. */
export function registerPublicActivityPubRoutes(
  app: Hono,
  dependencies: PublicActivityPubRouteDependencies,
): void {
  app.get("/users/:username", async (context) => {
    const username = context.req.param("username");
    const actor = await dependencies.renders.buildRegisteredUserActorDocument(username);
    if (actor == null) {
      return context.json({ error: "user actor not found" }, 404);
    }
    return activityJsonResponse(await actor.toJsonLd());
  });

  app.get("/communities/:slug", async (context) => {
    return await renderLocalCommunityActor(context, dependencies);
  });

  app.get("/c/:slug", async (context) => {
    return await renderLocalCommunityActor(context, dependencies);
  });

  app.get("/communities/:slug/outbox", async (context) => {
    const slug = context.req.param("slug");
    const community = await dependencies.reads.loadLocalCommunityIdentity(slug);
    if (community == null) {
      return context.json({ error: "community actor not found" }, 404);
    }
    return activityJsonResponse(
      await buildEmptyOrderedCollection(
        new URL(`/communities/${slug}/outbox`, dependencies.publicConfig.fedifyOrigin),
      ).toJsonLd(),
    );
  });

  app.get("/communities/:slug/followers", async (context) => {
    const slug = context.req.param("slug");
    const community = await dependencies.reads.loadLocalCommunityIdentity(slug);
    if (community == null) {
      return context.json({ error: "community actor not found" }, 404);
    }
    return activityJsonResponse(
      await buildEmptyOrderedCollection(
        new URL(`/communities/${slug}/followers`, dependencies.publicConfig.fedifyOrigin),
      ).toJsonLd(),
    );
  });

  app.get("/communities/:slug/post/:objectId", async (context) => {
    const object = await loadPublishedObjectForRequest(dependencies, context.req.path);
    if (object == null || object.kind !== "post") {
      return context.json({ error: "published post not found" }, 404);
    }
    return activityJsonResponse(buildPublishedActivityObjectJson(object));
  });

  app.get("/communities/:slug/comment/:objectId", async (context) => {
    const object = await loadPublishedObjectForRequest(dependencies, context.req.path);
    if (object == null || object.kind !== "comment") {
      return context.json({ error: "published comment not found" }, 404);
    }
    return activityJsonResponse(buildPublishedActivityObjectJson(object));
  });

  app.get("/users/:username/outbox", async (context) => {
    return await renderUserOutbox(dependencies, context, context.req.param("username"));
  });

  app.get("/users/:username/followers", async (context) => {
    return await renderUserFollowers(dependencies, context, context.req.param("username"));
  });

  app.get("/users/:username/activities/create/post/:activityId", async (context) => {
    const activity = await loadPublishedCreateActivityForRequest(
      dependencies,
      context.req.path,
      context.req.param("username"),
      "post",
    );
    if (activity == null) {
      return context.json({ error: "published create activity not found" }, 404);
    }
    return activityJsonResponse(buildPublishedCreateActivityJson(activity));
  });

  app.get("/users/:username/activities/create/comment/:activityId", async (context) => {
    const activity = await loadPublishedCreateActivityForRequest(
      dependencies,
      context.req.path,
      context.req.param("username"),
      "comment",
    );
    if (activity == null) {
      return context.json({ error: "published create activity not found" }, 404);
    }
    return activityJsonResponse(buildPublishedCreateActivityJson(activity));
  });

  app.get("/users/:username/post/:objectId", async (context) => {
    const object = await loadPublishedObjectForRequest(dependencies, context.req.path);
    if (object == null || object.kind !== "post") {
      return context.json({ error: "published post not found" }, 404);
    }
    return activityJsonResponse(buildPublishedActivityObjectJson(object));
  });

  app.get("/users/:username/comment/:objectId", async (context) => {
    const object = await loadPublishedObjectForRequest(dependencies, context.req.path);
    if (object == null || object.kind !== "comment") {
      return context.json({ error: "published comment not found" }, 404);
    }
    return activityJsonResponse(buildPublishedActivityObjectJson(object));
  });

  app.get("/actors/:identifier/outbox", async (context) => {
    const identifier = context.req.param("identifier");
    if ((await dependencies.reads.loadUserActorIdentity(identifier)) != null) {
      return await renderUserOutbox(dependencies, context, identifier);
    }
    if (!(await dependencies.reads.hasLocalActor(identifier))) {
      return context.json({ error: "actor not found" }, 404);
    }
    return activityJsonResponse(
      await buildEmptyOrderedCollection(
        new URL(`/actors/${identifier}/outbox`, dependencies.publicConfig.fedifyOrigin),
      ).toJsonLd(),
    );
  });

  app.get("/actors/:identifier/followers", async (context) => {
    const identifier = context.req.param("identifier");
    if ((await dependencies.reads.loadUserActorIdentity(identifier)) != null) {
      return await renderUserFollowers(dependencies, context, identifier);
    }
    if (!(await dependencies.reads.hasLocalActor(identifier))) {
      return context.json({ error: "actor not found" }, 404);
    }
    return activityJsonResponse(
      await buildEmptyOrderedCollection(
        new URL(`/actors/${identifier}/followers`, dependencies.publicConfig.fedifyOrigin),
      ).toJsonLd(),
    );
  });
}

async function renderLocalCommunityActor(
  context: Context,
  dependencies: PublicActivityPubRouteDependencies,
): Promise<Response> {
  const slug = context.req.param("slug") ?? "";
  const actor = await dependencies.renders.buildLocalCommunityActorDocument(slug);
  if (actor == null) {
    return context.json({ error: "community actor not found" }, 404);
  }
  return activityJsonResponse(await actor.toJsonLd());
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
  const pageMarker = context.req.query("page");
  const requestedPageNumber = context.req.query("page_number");
  const isPageRequest = pageMarker != null;
  if (isPageRequest && pageMarker !== "true") {
    return context.json({ error: "invalid pagination" }, 400);
  }

  // The public ActivityStreams contract has a fixed page size. Keeping the
  // size out of client control makes page URLs stable across peers and releases.
  const pageSize = 40;
  const page = requestedPageNumber == null ? 1 : Number(requestedPageNumber);
  if (!Number.isInteger(page) || page < 1 || (!isPageRequest && requestedPageNumber != null)) {
    return context.json({ error: "invalid pagination" }, 400);
  }

  let data;
  try {
    data = await dependencies.reads.loadUserFollowers(username, page, pageSize);
  } catch {
    // A failed persistence read must never be rendered as an empty social graph.
    return context.json({ error: "followers collection unavailable" }, 502);
  }
  if (data == null) {
    return context.json({ error: "user actor not found" }, 404);
  }

  const id = data.followersUrl;
  if (!isPageRequest) {
    return activityJsonResponse({
      "@context": "https://www.w3.org/ns/activitystreams",
      type: "OrderedCollection",
      id,
      totalItems: data.totalItems,
      ...(data.totalItems > 0 ? { first: `${id}?page=true` } : {}),
    });
  }

  const totalPages = Math.ceil(data.totalItems / pageSize);
  if (page > totalPages || totalPages === 0) {
    return context.json({ error: "followers page not found" }, 404);
  }
  const pageId = page === 1 ? `${id}?page=true` : `${id}?page=true&page_number=${page}`;
  return activityJsonResponse({
    "@context": "https://www.w3.org/ns/activitystreams",
    type: "OrderedCollectionPage",
    id: pageId,
    partOf: id,
    totalItems: data.totalItems,
    orderedItems: data.actorIds,
    ...(page > 1 ? { prev: `${id}?page=true&page_number=${page - 1}` } : {}),
    ...(page < totalPages ? { next: `${id}?page=true&page_number=${page + 1}` } : {}),
  });
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
  username: string,
  kind: "post" | "comment",
): Promise<PublishedActivityObjectView | null> {
  // The delivered Create.id is a full URL. Reconstruct it from the request path
  // so dereference uses the exact same durable id Python stored after publish.
  const activityUrl = new URL(requestPath, dependencies.publicConfig.fedifyOrigin).href;
  const activity = await dependencies.reads.loadPublishedActivityObjectByActivityId(activityUrl);
  if (activity == null || activity.actorUsername !== username || activity.kind !== kind) {
    return null;
  }
  return activity;
}
