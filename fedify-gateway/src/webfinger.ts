import type { GatewayConfig } from "./config.js";
import {
  getBridgeActorIdentity,
  loadLocalCommunityIdentity,
  loadUserActorIdentity,
} from "./actor-store.js";

export interface WebFingerDocument {
  // The gateway only emits the minimal JRD shape Lemmy and similar consumers
  // need to discover one local actor.
  subject: string;
  aliases: string[];
  links: Array<{
    rel: "self";
    type: "application/activity+json";
    href: string;
  }>;
}

export async function buildWebFingerDocument(
  config: GatewayConfig,
  resource: string,
): Promise<WebFingerDocument | null> {
  // WebFinger discovery is stricter than generic actor existence checks:
  // handles must match one concrete actor kind and must not silently fall back
  // from user lookup into community lookup.
  const localHost = new URL(config.fedifyOrigin).host;
  const bridgeIdentity = getBridgeActorIdentity(config);

  if (resource === `acct:${config.actorIdentifier}@${localHost}`) {
    return buildDocument(resource, [bridgeIdentity.actorId.href], bridgeIdentity.actorId.href);
  }

  const communitySlug = parseLocalCommunityResource(resource, localHost);
  if (communitySlug != null) {
    const communityIdentity = await loadLocalCommunityIdentity(config, communitySlug);
    if (communityIdentity == null) {
      return null;
    }
    return buildDocument(
      resource,
      [
        communityIdentity.actorId.href,
        new URL(`/c/${communitySlug}`, config.fedifyOrigin).href,
      ],
      communityIdentity.actorId.href,
    );
  }

  const username = parseLocalUserResource(resource, localHost);
  if (username == null) {
    return null;
  }
  const userIdentity = await loadUserActorIdentity(config, username);
  if (userIdentity != null) {
    return buildDocument(resource, [userIdentity.actorId.href], userIdentity.actorId.href);
  }

  // Lemmy can search communities by the plain slug form as well as the `!slug`
  // handle. When the plain form is not claimed by a registered local user, use
  // it as a community alias so discovery stays robust.
  const communityIdentity = await loadLocalCommunityIdentity(config, username);
  if (communityIdentity == null) {
    return null;
  }
  return buildDocument(
    resource,
    [
      communityIdentity.actorId.href,
      new URL(`/c/${username}`, config.fedifyOrigin).href,
    ],
    communityIdentity.actorId.href,
  );
}

function buildDocument(
  subject: string,
  aliases: string[],
  href: string,
): WebFingerDocument {
  return {
    subject,
    aliases,
    links: [
      {
        rel: "self",
        type: "application/activity+json",
        href,
      },
    ],
  };
}

function parseLocalUserResource(
  resource: string,
  localHost: string,
): string | null {
  const handle = parseAcctResource(resource, localHost);
  if (handle == null || handle.startsWith("!")) {
    return null;
  }
  return handle;
}

function parseLocalCommunityResource(
  resource: string,
  localHost: string,
): string | null {
  const handle = parseAcctResource(resource, localHost);
  if (handle == null || !handle.startsWith("!")) {
    return null;
  }
  const slug = handle.slice(1);
  return slug.length > 0 ? slug : null;
}

function parseAcctResource(
  resource: string,
  localHost: string,
): string | null {
  if (!resource.startsWith("acct:")) {
    return null;
  }
  const handle = resource.slice("acct:".length);
  const parts = handle.split("@");
  if (parts.length !== 2 || parts[0].length === 0 || parts[1] !== localHost) {
    return null;
  }
  return parts[0];
}
