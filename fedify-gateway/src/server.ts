/**
 * Gateway HTTP composition root.
 *
 * This module wires Fedify, debug logging, public read/render adapters, and
 * route registration order. Route handler bodies live under src/routes so this
 * file remains focused on process composition and trusted dependency closure.
 */
import { pathToFileURL } from "node:url";

import { serve } from "@hono/node-server";
import { federation as fedifyMiddleware } from "@fedify/hono";
import { Hono } from "hono";

import {
  buildLocalCommunityGroupActor,
  buildUserPersonActor,
} from "./actors.js";
import {
  hasLocalActor,
  loadLocalCommunityIdentity,
  loadUserActorIdentity,
} from "./actor-store.js";
import {
  type GatewayConfig,
  type GatewayContextData,
  loadConfig,
} from "./config.js";
import { initializeDebugFileLog, installDebugFetchLogging } from "./debug-file-log.js";
import { createGatewayFederation } from "./federation.js";
import { buildLocalCommunityPublicKeyCarrier } from "./local-community-keys.js";
import { APP_VERSION } from "./project-version.js";
import {
  listLocalCommunities,
  loadPublishedActivityObjectByActivityId,
  loadPublishedActivityObjectByObjectId,
  loadUserFollowers,
  loadUserOutbox,
} from "./python-bridge-client.js";
import { registerHealthRoutes } from "./routes/health.js";
import { buildActivityPubRequestData, registerInboxLoggingMiddleware } from "./routes/inbox-logging.js";
import { registerInternalDeliveryRoutes } from "./routes/internal-delivery.js";
import { registerInternalFollowRoutes } from "./routes/internal-follow.js";
import { registerInternalProfileRoutes } from "./routes/internal-profile.js";
import { registerInternalPublishRoutes } from "./routes/internal-publish.js";
import { registerPublicActivityPubRoutes, type PublicRouteConfig } from "./routes/public-activitypub.js";
import { registerPublicDiscoveryRoutes } from "./routes/public-discovery.js";
import { buildWebFingerDocument } from "./webfinger.js";

/** Build the Hono gateway application with route modules mounted in order. */
export function createGatewayApp(config: GatewayConfig): Hono {
  const fedify = createGatewayFederation(config);
  const app = new Hono();
  const isDebug = config.logLevel === "debug";
  const debugLogPath = initializeDebugFileLog(config);
  installDebugFetchLogging(config);
  if (debugLogPath != null) {
    console.log(`[DebugFileLog] Writing gateway debug diagnostics to ${debugLogPath}`);
  }

  installDebugAccessLogging(app, isDebug);

  const publicConfig = toPublicRouteConfig(config);
  const internalDependencies = { config, fedify };

  registerHealthRoutes(app);
  registerPublicDiscoveryRoutes(app, {
    reads: {
      listLocalCommunities: () => listLocalCommunities(config),
      buildWebFingerDocument: (resource) => buildWebFingerDocument(config, resource),
    },
  });
  registerPublicActivityPubRoutes(app, {
    publicConfig,
    reads: {
      loadUserActorIdentity: (username) => loadUserActorIdentity(config, username),
      loadLocalCommunityIdentity: (slug) => loadLocalCommunityIdentity(config, slug),
      hasLocalActor: (identifier) => hasLocalActor(config, identifier),
      loadPublishedActivityObjectByObjectId: (objectUrl) =>
        loadPublishedActivityObjectByObjectId(config, objectUrl),
      loadPublishedActivityObjectByActivityId: (activityUrl) =>
        loadPublishedActivityObjectByActivityId(config, activityUrl),
      loadUserOutbox: (username, page, pageSize) =>
        loadUserOutbox(config, username, page, pageSize),
      loadUserFollowers: (username, page, pageSize) =>
        loadUserFollowers(config, username, page, pageSize),
    },
    renders: {
      buildRegisteredUserActorDocument: (username) =>
        buildRegisteredUserActorDocument(config, fedify, username),
      buildLocalCommunityActorDocument: (slug) =>
        buildLocalCommunityActorDocument(config, fedify, slug),
    },
  });
  registerInternalFollowRoutes(app, internalDependencies);
  registerInternalPublishRoutes(app, internalDependencies);
  registerInternalDeliveryRoutes(app, internalDependencies);
  registerInternalProfileRoutes(app, internalDependencies);
  registerInboxLoggingMiddleware(app, { isDebug });

  app.use(
    fedifyMiddleware(fedify, async (context): Promise<GatewayContextData> => {
      // Fedify still needs full context data for the existing inbound pipeline;
      // raw request metadata is attached only for POST /inbox recovery.
      const activitypubRequestData = await buildActivityPubRequestData(context);
      return { ...config, ...activitypubRequestData };
    }),
  );

  return app;
}

/** Start the gateway HTTP server using the configured production launch path. */
export function startGatewayServer(config: GatewayConfig = loadConfig()): void {
  const app = createGatewayApp(config);
  serve({
    fetch: app.fetch.bind(app),
    port: config.port,
  });

  console.log(
    `Fedify gateway version=${APP_VERSION} listening on ${config.fedifyOrigin} (port ${config.port}) and forwarding to ${`${config.pythonBridgeInternalUrl}/internal/activitypub/events`}`,
  );
}

if (isMainModule()) {
  startGatewayServer();
}

function installDebugAccessLogging(app: Hono, isDebug: boolean): void {
  if (!isDebug) {
    return;
  }
  // Debug access logging is intentionally gated because it can be noisy, but
  // it remains available to verify which ActivityPub URLs Mastodon fetches.
  app.use("*", async (context, next) => {
    const startedAt = Date.now();
    const method = context.req.method;
    const path = context.req.path;
    const accept = context.req.header("accept") ?? "";
    const userAgent = context.req.header("user-agent") ?? "";
    const forwardedFor =
      context.req.header("cf-connecting-ip") ??
      context.req.header("x-forwarded-for") ??
      "";

    await next();

    console.log("[HTTP]", {
      method,
      path,
      status: context.res.status,
      durationMs: Date.now() - startedAt,
      accept,
      userAgent,
      forwardedFor,
    });
  });
}

function toPublicRouteConfig(config: GatewayConfig): PublicRouteConfig {
  return {
    actorIdentifier: config.actorIdentifier,
    actorName: config.actorName,
    actorSummary: config.actorSummary,
    fedifyOrigin: config.fedifyOrigin,
    logLevel: config.logLevel,
  };
}

function isMainModule(): boolean {
  const entry = process.argv[1];
  return entry != null && import.meta.url === pathToFileURL(entry).href;
}

async function buildRegisteredUserActorDocument(
  config: GatewayConfig,
  fedify: ReturnType<typeof createGatewayFederation>,
  username: string,
) {
  const userIdentity = await loadUserActorIdentity(config, username);
  if (userIdentity == null) {
    return null;
  }
  const context = fedify.createContext(new URL(config.fedifyOrigin), config);
  return buildUserPersonActor(
    userIdentity,
    new URL("/inbox", config.fedifyOrigin),
    await context.getActorKeyPairs(`users/${username}`),
  );
}

async function buildLocalCommunityActorDocument(
  config: GatewayConfig,
  fedify: ReturnType<typeof createGatewayFederation>,
  slug: string,
) {
  const communityIdentity = await loadLocalCommunityIdentity(config, slug);
  if (communityIdentity == null) {
    return null;
  }
  return buildLocalCommunityGroupActor(
    communityIdentity,
    new URL("/inbox", config.fedifyOrigin),
    await buildLocalCommunityPublicKeyCarrier(communityIdentity),
  );
}
