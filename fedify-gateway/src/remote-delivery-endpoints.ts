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
  const classified = classifyRemoteHostname(hostname);
  return classified.kind !== "public";
}

function isLoopbackHostname(hostname: string): boolean {
  const classified = classifyRemoteHostname(hostname);
  return classified.kind === "loopback";
}

type RemoteHostnameClassification =
  | { kind: "public" }
  | { kind: "loopback" }
  | { kind: "blocked" };

function classifyRemoteHostname(hostname: string): RemoteHostnameClassification {
  if (hostname === "localhost") return { kind: "loopback" };
  if (hostname === "metadata.google.internal") return { kind: "blocked" };

  const mappedIpv4 = parseIpv4MappedIpv6(hostname);
  if (mappedIpv4 != null) {
    return classifyIpv4(mappedIpv4);
  }

  const ipVersion = isIP(hostname);
  if (ipVersion === 0) return { kind: "public" };
  return ipVersion === 4 ? classifyIpv4(hostname) : classifyIpv6(hostname);
}

function classifyIpv4(hostname: string): RemoteHostnameClassification {
  const octets = hostname.split(".").map((part) => Number.parseInt(part, 10));
  if (octets.length !== 4 || octets.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) {
    return { kind: "blocked" };
  }
  const [a, b] = octets;
  if (a === 127) return { kind: "loopback" };
  if (a === 0 || a === 10) return { kind: "blocked" };
  if (a === 169 && b === 254) return { kind: "blocked" };
  if (a === 172 && b >= 16 && b <= 31) return { kind: "blocked" };
  if (a === 192 && b === 168) return { kind: "blocked" };
  if (a === 100 && b >= 64 && b <= 127) return { kind: "blocked" };
  return { kind: "public" };
}

function classifyIpv6(hostname: string): RemoteHostnameClassification {
  const value = hostname.toLowerCase();
  if (value === "::1") return { kind: "loopback" };
  if (value === "::") return { kind: "blocked" };
  if (value.startsWith("fe80:") || value.startsWith("fc") || value.startsWith("fd")) {
    return { kind: "blocked" };
  }
  return { kind: "public" };
}

function parseIpv4MappedIpv6(hostname: string): string | null {
  const value = hostname.toLowerCase();
  const dotted = value.match(/^::ffff:(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})$/);
  if (dotted != null) return dotted[1];

  const hex = value.match(/^::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/);
  if (hex == null) return null;
  const high = Number.parseInt(hex[1], 16);
  const low = Number.parseInt(hex[2], 16);
  if (!Number.isInteger(high) || !Number.isInteger(low) || high < 0 || high > 0xffff || low < 0 || low > 0xffff) {
    return null;
  }
  return `${(high >> 8) & 0xff}.${high & 0xff}.${(low >> 8) & 0xff}.${low & 0xff}`;
}
