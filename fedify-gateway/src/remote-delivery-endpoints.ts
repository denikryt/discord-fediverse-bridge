/**
 * Admission policy for remote ActivityPub delivery endpoints.
 *
 * The ActivityPub actor document is the protocol source of inbox URLs, but it
 * is still attacker-controlled input from the network. This module keeps the
 * gateway from sending signed bridge traffic to local, private, credentialed,
 * or otherwise non-routable targets. Test fixtures may explicitly allow HTTP
 * loopback without weakening the production default.
 */
import { isIP } from "node:net";

export interface RemoteEndpointAdmissionOptions {
  /** Local bridge origin that must never be used as a remote delivery target. */
  localOrigin?: string | URL;
  /** Narrow test-only escape hatch for Fedify fixture servers on loopback HTTP. */
  allowHttpLoopbackForTests?: boolean;
}

/** Validate and normalize one remote actor inbox or sharedInbox URL. */
export function admitRemoteDeliveryEndpoint(
  endpoint: string,
  options: RemoteEndpointAdmissionOptions = {},
): string {
  const parsed = parseEndpoint(endpoint);
  const localOrigin = options.localOrigin == null
    ? null
    : new URL(options.localOrigin).origin;

  if (parsed.username || parsed.password) {
    throw new Error("remote delivery endpoint must not include credentials");
  }
  if (parsed.hash) {
    throw new Error("remote delivery endpoint must not include a fragment");
  }
  if (localOrigin != null && parsed.origin === localOrigin) {
    throw new Error("remote delivery endpoint must not target the local bridge origin");
  }

  const hostname = normalizeHostname(parsed.hostname);
  const loopback = isBlockedRemoteHostname(hostname);
  if (parsed.protocol === "http:") {
    if (!(options.allowHttpLoopbackForTests === true && isLoopbackHostname(hostname))) {
      throw new Error("remote delivery endpoint must use https");
    }
  } else if (parsed.protocol !== "https:") {
    throw new Error("remote delivery endpoint must use https");
  }

  if (
    loopback &&
    !(options.allowHttpLoopbackForTests === true && parsed.protocol === "http:" && isLoopbackHostname(hostname))
  ) {
    throw new Error("remote delivery endpoint must not target local or private networks");
  }
  return parsed.href;
}

function parseEndpoint(endpoint: string): URL {
  try {
    return new URL(endpoint);
  } catch (error) {
    throw new Error(
      `remote delivery endpoint must be an absolute URL: ${(error as Error).message}`,
    );
  }
}

function normalizeHostname(hostname: string): string {
  return hostname.replace(/^\[|\]$/g, "").toLowerCase();
}

function isBlockedRemoteHostname(hostname: string): boolean {
  if (isLoopbackHostname(hostname)) return true;
  if (hostname === "localhost" || hostname === "metadata.google.internal") {
    return true;
  }
  const ipVersion = isIP(hostname);
  if (ipVersion === 0) return false;
  return ipVersion === 4 ? isBlockedIpv4(hostname) : isBlockedIpv6(hostname);
}

function isLoopbackHostname(hostname: string): boolean {
  if (hostname === "localhost" || hostname === "::1") return true;
  if (hostname.startsWith("127.")) return true;
  return false;
}

function isBlockedIpv4(hostname: string): boolean {
  const octets = hostname.split(".").map((part) => Number.parseInt(part, 10));
  if (octets.length !== 4 || octets.some((part) => !Number.isInteger(part))) {
    return true;
  }
  const [a, b] = octets;
  if (a === 0 || a === 10 || a === 127) return true;
  if (a === 169 && b === 254) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 192 && b === 168) return true;
  if (a === 100 && b >= 64 && b <= 127) return true;
  return false;
}

function isBlockedIpv6(hostname: string): boolean {
  const value = hostname.toLowerCase();
  if (value === "::" || value === "::1") return true;
  if (value.startsWith("fe80:") || value.startsWith("fc") || value.startsWith("fd")) return true;
  return false;
}
