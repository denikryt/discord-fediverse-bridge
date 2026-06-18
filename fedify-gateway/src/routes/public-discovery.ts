/**
 * Public discovery route registration for WebFinger and bridge community lists.
 *
 * The module receives only non-secret display configuration and read ports so
 * unauthenticated request handling cannot access internal bearer material.
 */
import type { Hono } from "hono";

export interface LocalCommunityDiscoveryRecord {
  id: number;
  slug: string;
  name: string;
  title: string;
  description: string | null;
  actor_id: string;
  alternate_actor_id: string;
  handle: string;
}

export interface WebFingerDocument {
  subject: string;
  aliases?: string[];
  links: Array<Record<string, unknown>>;
}

export interface PublicDiscoveryReadPort {
  listLocalCommunities(): Promise<LocalCommunityDiscoveryRecord[]>;
  buildWebFingerDocument(resource: string): Promise<WebFingerDocument | null>;
}

export interface PublicDiscoveryRouteDependencies {
  reads: PublicDiscoveryReadPort;
}

/** Register public discovery endpoints without exposing internal bridge auth. */
export function registerPublicDiscoveryRoutes(
  app: Hono,
  dependencies: PublicDiscoveryRouteDependencies,
): void {
  app.get("/.well-known/discord-fediverse-bridge/communities", async (context) => {
    // The route preserves the current public discovery shape while moving the
    // Python-backed read behind a composition-owned port.
    const communities = await dependencies.reads.listLocalCommunities();
    return context.json({
      software: "discord-fediverse-bridge",
      communities,
    });
  });

  app.get("/.well-known/webfinger", async (context) => {
    const resource = context.req.query("resource");
    if (!resource) {
      return context.json({ error: "resource query parameter is required" }, 400);
    }

    const webFingerDocument = await dependencies.reads.buildWebFingerDocument(resource);
    if (webFingerDocument == null) {
      return context.json({ error: "resource not found" }, 404);
    }
    return context.newResponse(JSON.stringify(webFingerDocument), 200, {
      "Content-Type": "application/jrd+json",
    });
  });
}
