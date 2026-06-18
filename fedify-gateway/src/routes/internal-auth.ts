/**
 * Internal gateway bearer-admission helper.
 *
 * The Python runtime is the only client for these command routes.  Keep the
 * exact bearer comparison centralized without broadening accepted auth syntax.
 */
import type { GatewayConfig } from "../config.js";

/** Return true only for the configured Python bridge bearer token. */
export function hasValidInternalAuthorization(
  config: GatewayConfig,
  authorizationHeader: string | null,
): boolean {
  return authorizationHeader === `Bearer ${config.pythonBridgeSharedSecret}`;
}
