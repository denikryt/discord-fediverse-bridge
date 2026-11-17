/** Independently validated, tokenless Fluxer instance discovery.
 *
 * A vetted DNS address is pinned for this discovery request. The actual SDK
 * REST/Gateway egress will require its own equivalent policy before bot login;
 * this file does not claim to secure independently resolved SDK connections.
 */
import {lookup as dnsLookup} from 'node:dns/promises';
import {request as httpsRequest} from 'node:https';
import {request as httpRequest} from 'node:http';
import {BlockList, isIP} from 'node:net';

export type DiscoveryPolicy = {
  allowInsecureLocalDevelopment?: boolean;
  allowedEndpointHosts?: readonly string[];
  resolver?: (host: string) => Promise<readonly {address: string; family: number}[]>;
};
export type ValidatedDiscovery = {
  origin: string;
  apiPublic: string;
  gateway: string;
  endpoints: Readonly<Record<string, string>>;
};

const forbidden = new BlockList();
for (const [address, bits] of [
  ['0.0.0.0', 8], ['10.0.0.0', 8], ['100.64.0.0', 10], ['127.0.0.0', 8],
  ['169.254.0.0', 16], ['172.16.0.0', 12], ['192.0.0.0', 24],
  ['192.168.0.0', 16], ['198.18.0.0', 15], ['224.0.0.0', 4],
  ['240.0.0.0', 4],
] as const) forbidden.addSubnet(address, bits, 'ipv4');
for (const [address, bits] of [
  ['::', 128], ['::1', 128], ['::ffff:0:0', 96], ['100::', 64],
  ['2001:db8::', 32], ['fc00::', 7], ['fe80::', 10], ['ff00::', 8],
] as const) forbidden.addSubnet(address, bits, 'ipv6');

function permittedAddress(address: string, development: boolean): boolean {
  /** Every DNS answer must be public, except explicit loopback-only tests. */
  const family = isIP(address);
  if (family !== 4 && family !== 6) return false;
  if (development && (address === '127.0.0.1' || address === '::1')) return true;
  return !forbidden.check(address, family === 4 ? 'ipv4' : 'ipv6');
}


/** Parse an exact origin, refusing userinfo, paths, fragments and ambiguity. */
export function validateOrigin(raw: string, policy: DiscoveryPolicy = {}): URL {
  if (typeof raw !== 'string' || !/^https?:\/\/[^/?#]+$/i.test(raw) ||
      raw !== raw.trim()) throw new Error('invalid Fluxer discovery origin');
  let url: URL;
  try { url = new URL(raw); }
  catch { throw new Error('invalid Fluxer discovery origin'); }
  // Python's persisted canonical origin strips a DNS root dot. Refuse this
  // ambiguous form before SDK login so READY identity cannot diverge from DB.
  if (!url.hostname || url.hostname.endsWith('.') || url.username || url.password ||
      url.search || url.hash ||
      (url.protocol !== 'https:' && url.protocol !== 'http:')) {
    throw new Error('invalid Fluxer discovery origin');
  }
  const local = url.hostname === 'localhost' || url.hostname === '127.0.0.1' ||
    url.hostname === '[::1]';
  if (url.protocol !== 'https:' && !(local && policy.allowInsecureLocalDevelopment)) {
    throw new Error('Fluxer discovery requires HTTPS');
  }
  return url;
}

/** Strictly inspect SDK-discovered endpoints, never substitute first-party API. */
export function validateDiscoveryDocument(
  origin: string, document: unknown, policy: DiscoveryPolicy = {},
): ValidatedDiscovery {
  const requested = validateOrigin(origin, policy);
  if (!document || typeof document !== 'object' || Array.isArray(document)) {
    throw new Error('invalid Fluxer discovery document');
  }
  const endpoints = (document as Record<string, unknown>).endpoints;
  if (!endpoints || typeof endpoints !== 'object' || Array.isArray(endpoints)) {
    throw new Error('Fluxer discovery endpoints missing');
  }
  const values = endpoints as Record<string, unknown>;
  if (typeof values.api_public !== 'string') {
    throw new Error('Fluxer public bot API missing');
  }
  const api = new URL(values.api_public);
  if (api.username || api.password || api.search || api.hash ||
      (api.protocol !== 'https:' && !(policy.allowInsecureLocalDevelopment &&
        (api.hostname === 'localhost' || api.hostname === '127.0.0.1' ||
         api.hostname === '[::1]')))) {
    throw new Error('invalid Fluxer bot API URL');
  }
  // The trusted instance supplies a Gateway host independently from REST.
  // Both must be approved before an SDK client can receive the bot token.
  if (typeof values.gateway !== 'string') {
    throw new Error('Fluxer Gateway endpoint missing');
  }
  let gateway: URL;
  try {gateway = new URL(values.gateway);}
  catch {throw new Error('invalid Fluxer Gateway URL');}
  const gatewayLocal = gateway.hostname === '127.0.0.1' ||
    gateway.hostname === 'localhost' || gateway.hostname === '[::1]';
  if (gateway.username || gateway.password || gateway.search || gateway.hash ||
      (gateway.protocol !== 'wss:' && !(policy.allowInsecureLocalDevelopment &&
        gatewayLocal && gateway.protocol === 'ws:'))) {
    throw new Error('invalid Fluxer Gateway URL');
  }
  if (gateway.hostname !== requested.hostname &&
      !(policy.allowedEndpointHosts ?? []).includes(gateway.hostname)) {
    throw new Error('unapproved Fluxer Gateway host');
  }
  if (api.hostname !== requested.hostname &&
      !(policy.allowedEndpointHosts ?? []).includes(api.hostname)) {
    throw new Error('unapproved Fluxer bot API host');
  }
  // A malformed unrelated endpoint cannot be silently interpreted as a bot URL.
  const exact: Record<string, string> = {};
  for (const [key, value] of Object.entries(values)) {
    if (typeof value !== 'string') continue;
    exact[key] = value;
  }
  return {origin: requested.origin, apiPublic: api.toString(),
    gateway: gateway.toString(), endpoints: exact};
}

/** One bounded tokenless GET with pinned DNS, no redirects or bearer headers. */
export async function discoverInstance(
  origin: string, policy: DiscoveryPolicy = {},
): Promise<ValidatedDiscovery> {
  const url = validateOrigin(origin, policy);
  const local = url.protocol === 'http:' && policy.allowInsecureLocalDevelopment === true;
  const answers = await (policy.resolver ?? (async host =>
    dnsLookup(host, {all: true, verbatim: true})))(url.hostname);
  if (!Array.isArray(answers) || !answers.length ||
      answers.some(answer => !permittedAddress(answer.address, local))) {
    throw new Error('Fluxer discovery DNS target not permitted');
  }
  const selected = answers[0];
  const target = new URL('/.well-known/fluxer', url.origin);
  const query = url.protocol === 'https:' ? httpsRequest : httpRequest;
  const body = await new Promise<string>((resolve, reject) => {
    const request = query(target, {
      timeout: 5000,
      // Keep DNS authorization bound to the actual socket; the Host header
      // and HTTPS certificate verification still reference the original name.
      lookup: (_host, _options, callback) => callback(null, selected.address, selected.family),
      headers: {'accept': 'application/json'},
      maxHeaderSize: 8192,
    }, response => {
      if (response.statusCode !== 200) {
        response.resume(); reject(new Error('Fluxer discovery unexpected HTTP status')); return;
      }
      let result = '';
      let bytes = 0;
      response.setEncoding('utf8');
      response.on('data', (chunk: string) => {
        bytes += Buffer.byteLength(chunk);
        if (bytes > 64 * 1024) {
          response.destroy(new Error('Fluxer discovery response too large')); return;
        }
        result += chunk;
      });
      response.once('end', () => resolve(result));
      response.once('error', reject);
    });
    request.once('timeout', () => request.destroy(new Error('Fluxer discovery timeout')));
    request.once('error', reject);
    request.end();
  });
  let document: unknown;
  try {document = JSON.parse(body);}
  catch {throw new Error('invalid Fluxer discovery JSON');}
  return validateDiscoveryDocument(origin, document, policy);
}

/** Guard the actual SDK-created Gateway socket after `/gateway/bot` discovery.
 * SDK v3.3.0 appends `v=1&encoding=json`; no second client or heartbeat is built.
 */
export function validateGatewayConnection(raw: string, approvedGateway: string): void {
  let actual: URL;
  let approved: URL;
  try {actual = new URL(raw); approved = new URL(approvedGateway);}
  catch {throw new Error('invalid Fluxer Gateway socket URL');}
  if (actual.protocol !== approved.protocol || actual.host !== approved.host ||
      actual.username || actual.password || actual.hash ||
      !['ws:', 'wss:'].includes(actual.protocol)) {
    throw new Error('unapproved Fluxer Gateway socket destination');
  }
  const params = actual.searchParams;
  if (params.size !== 2 || params.get('v') !== '1' ||
      params.get('encoding') !== 'json') {
    throw new Error('unexpected Fluxer Gateway socket query');
  }
}
