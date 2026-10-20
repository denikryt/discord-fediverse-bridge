/**
 * Pure URL helpers for bridge-owned ActivityPub identifiers.
 *
 * These helpers are secret-free and preserve FEDIFY_ORIGIN slash handling for
 * outbound use-case modules that construct canonical bridge URLs.
 */

import type { GatewayConfig } from "../config.js";

/**
 * Build one bridge-owned URL under FEDIFY_ORIGIN from an absolute path.
 *
 * This helper preserves the invariant that outbound ActivityPub ids stay on
 * the bridge domain regardless of whether FEDIFY_ORIGIN has a trailing slash.
 */
export function buildGatewayUrl(config: GatewayConfig, absolutePath: string): URL {
  // Prefix a slash defensively so callers can pass either `activities/...` or
  // `/activities/...` without risking host/path concatenation.
  const path = absolutePath.startsWith("/") ? absolutePath : `/${absolutePath}`;
  return new URL(path, config.fedifyOrigin);
}

/** Build the canonical typed ActivityPub actor URL for one registered bridge user. */
export function buildUserActorUri(config: GatewayConfig, actorUsername: string): URL {
  // typed ActivityPub identity migration makes user actor identity live under /actors/users/{username}.
  return buildGatewayUrl(config, `/actors/users/${actorUsername}`);
}
