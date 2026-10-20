/**
 * Admission policy for remote ActivityPub delivery endpoints.
 *
 * The ActivityPub actor document is the protocol source of inbox URLs, but it
 * is still attacker-controlled input from the network. This module keeps the
 * gateway from sending signed bridge traffic to local, private, credentialed,
 * or otherwise non-routable targets. Test fixtures may explicitly allow HTTP
 * loopback without weakening the production default.
 */
import { lookup as dnsLookup, type LookupAddress } from "node:dns";
import { BlockList, isIP, type LookupFunction } from "node:net";

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
  return classifyRemoteHostname(hostname).kind !== "public";
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
    return isPublicRemoteAddress(mappedIpv4) ? { kind: "public" } : { kind: "blocked" };
  }

  const ipVersion = isIP(hostname);
  if (ipVersion === 0) return { kind: "public" };
  if (hostname === "127.0.0.1" || hostname === "::1") return { kind: "loopback" };
  return isPublicRemoteAddress(hostname) ? { kind: "public" } : { kind: "blocked" };
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


/** Public-only socket resolver used by outbound HTTPS, not merely URL admission. */
export const publicOnlyLookup: LookupFunction = (hostname, options, callback) => {
  // The socket connects to the exact address returned by this callback, which
  // closes the DNS-rebinding gap between preliminary validation and TLS dial.
  dnsLookup(hostname, { all: true, verbatim: true }, (error, addresses) => {
    if (error) {
      callback(error, "", 0);
      return;
    }
    try {
      // Respect socket family selection without accepting any private answer.
      validateResolvedRemoteAddresses(addresses);
      const admitted = addresses.filter((item) => !options.family || item.family === options.family);
      if (admitted.length === 0) throw new Error("no resolved address matches required socket family");
      if (options.all) {
        callback(null, admitted);
      } else {
        callback(null, admitted[0].address, admitted[0].family);
      }
    } catch (cause) {
      callback(Object.assign(new Error("remote address is not publicly routable"), {
        code: "EADDRNOTAVAIL", cause,
      }), "", 0);
    }
  });
};

// IANA special-use IPv4 and special-purpose IPv6 addresses are not valid
// federation egress targets. Restrict IPv6 to global unicast and remove
// documentation, translation and tunneling ranges that could hide local IPs.
const blockedIpv4 = new BlockList();
for (const [network, prefix] of [
  ["0.0.0.0", 8], ["10.0.0.0", 8], ["100.64.0.0", 10],
  ["127.0.0.0", 8], ["169.254.0.0", 16], ["172.16.0.0", 12],
  ["192.0.0.0", 24], ["192.0.2.0", 24], ["192.88.99.0", 24],
  ["192.168.0.0", 16], ["198.18.0.0", 15], ["198.51.100.0", 24],
  ["203.0.113.0", 24], ["224.0.0.0", 4], ["240.0.0.0", 4],
] as const) blockedIpv4.addSubnet(network, prefix, "ipv4");

const blockedIpv6 = new BlockList();
for (const [network, prefix] of [
  ["2001::", 23], ["2001:db8::", 32], ["2002::", 16],
] as const) blockedIpv6.addSubnet(network, prefix, "ipv6");

/** Reject a mixed DNS answer rather than choosing a public result among private IPs. */
export function validateResolvedRemoteAddresses(
  addresses: readonly Pick<LookupAddress, "address" | "family">[],
): { address: string; family: 4 | 6 } {
  if (addresses.length === 0) {
    throw new Error("remote destination DNS lookup returned no addresses");
  }
  for (const address of addresses) {
    if (isIP(address.address) !== address.family || !isPublicRemoteAddress(address.address)) {
      throw new Error("remote destination DNS result contains a non-public address");
    }
  }
  const chosen = addresses[0];
  return { address: chosen.address, family: chosen.family === 6 ? 6 : 4 };
}

/** Admit only globally routable address families after DNS resolution. */
function isPublicRemoteAddress(address: string): boolean {
  const version = isIP(address);
  if (version === 4) return !blockedIpv4.check(address, "ipv4");
  if (version !== 6) return false;

  const mapped = parseIpv4MappedIpv6(address);
  if (mapped != null) return isPublicRemoteAddress(mapped);
  const firstGroup = Number.parseInt(address.split(":", 1)[0], 16);
  if (!Number.isInteger(firstGroup) || firstGroup < 0x2000 || firstGroup > 0x3fff) return false;
  return !blockedIpv6.check(address, "ipv6");
}
