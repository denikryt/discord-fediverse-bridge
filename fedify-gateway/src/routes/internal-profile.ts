/**
 * Internal profile-render route registration.
 *
 * Profile Update rendering keeps its canonical identity checks at the gateway
 * boundary because the signed ActivityPub actor document must match stored user
 * actor state exactly.
 */
import { Update } from "@fedify/vocab";
import type { Hono } from "hono";

import { buildUserPersonActor } from "../actors.js";
import { loadUserActorIdentity } from "../actor-store.js";
import type { GatewayConfig } from "../config.js";
import type { createGatewayFederation } from "../federation.js";
import type { RenderUserActorProfileUpdateRequest } from "../types.js";
import { hasValidInternalAuthorization } from "./internal-auth.js";

export interface InternalProfileRouteDependencies {
  config: GatewayConfig;
  fedify: ReturnType<typeof createGatewayFederation>;
}

/** Mount the profile Update renderer with its existing identity guard. */
export function registerInternalProfileRoutes(
  app: Hono,
  dependencies: InternalProfileRouteDependencies,
): void {
  const { config, fedify } = dependencies;

  app.post("/render-user-actor-profile-update", async (context) => {
    if (!hasValidInternalAuthorization(config, context.req.header("Authorization") ?? null)) {
      return context.json({ error: "invalid authorization" }, { status: 401 });
    }
    const payload = (await context.req.json()) as Partial<RenderUserActorProfileUpdateRequest>;
    const snapshot = payload.profileSnapshot;
    if (typeof payload.activityId !== "string" || snapshot == null || typeof snapshot !== "object") {
      return context.json({ error: "activityId and profileSnapshot are required" }, { status: 400 });
    }
    try {
      if (
        snapshot.schemaVersion !== 1 ||
        typeof snapshot.username !== "string" ||
        typeof snapshot.actorId !== "string" ||
        typeof snapshot.inboxId !== "string" ||
        typeof snapshot.outboxId !== "string" ||
        typeof snapshot.followersId !== "string" ||
        typeof snapshot.publicKeyId !== "string" ||
        typeof snapshot.publicKeyPem !== "string" ||
        typeof snapshot.displayName !== "string" ||
        !Array.isArray(snapshot.fields)
      ) {
        return context.json({ error: "invalid profile snapshot schema" }, { status: 400 });
      }
      const current = await loadUserActorIdentity(config, snapshot.username);
      if (
        current == null ||
        current.username !== snapshot.username ||
        current.actorId.href !== snapshot.actorId ||
        current.inboxId.href !== snapshot.inboxId ||
        current.outboxId.href !== snapshot.outboxId ||
        current.followersId.href !== snapshot.followersId ||
        `${snapshot.actorId}#main-key` !== snapshot.publicKeyId ||
        current.publicKeyPem !== snapshot.publicKeyPem
      ) {
        return context.json(
          { error: "profile snapshot identity does not match the canonical User Actor" },
          { status: 409 },
        );
      }
      const fedifyContext = fedify.createContext(new URL(config.fedifyOrigin), config);
      const person = buildUserPersonActor(
        {
          ...current,
          displayName: snapshot.displayName,
          summary: snapshot.summary,
          fields: snapshot.fields,
          avatar: snapshot.avatar == null ? null : { ...snapshot.avatar, url: new URL(snapshot.avatar.url) },
          header: snapshot.header == null ? null : { ...snapshot.header, url: new URL(snapshot.header.url) },
        },
        new URL("/inbox", config.fedifyOrigin),
        await fedifyContext.getActorKeyPairs(snapshot.username),
      );
      const activity = new Update({
        id: new URL(payload.activityId),
        actor: new URL(snapshot.actorId),
        object: person,
        tos: [new URL("https://www.w3.org/ns/activitystreams#Public")],
        ccs: [new URL(snapshot.followersId)],
      });
      return context.json({ activityJson: await activity.toJsonLd() });
    } catch (error) {
      return context.json({ error: error instanceof Error ? error.message : String(error) }, { status: 400 });
    }
  });
}
