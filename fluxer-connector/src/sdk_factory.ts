/** Produce exactly one official SDK Client for a previously vetted trusted instance.
 *
 * Only the operator-approved REST API and Gateway endpoints are used for
 * bot traffic. The SDK still owns REST, login, reconnect, heartbeats and events.
 */
import {validateGatewayConnection, validateOrigin, type ValidatedDiscovery} from './discovery.ts';

/** SDK constructor options used by pinned @fluxerjs/core v3.3.0. */
type TrustedClientOptions = {
  instance: Record<string, string> & {api_public: string; gateway: string};
  rest: {retries: number};
  WebSocket: new (url: string) => WebSocket;
};

/** Construct a Client without a second unvetted `fromDiscovery()` HTTP request. */
export function createTrustedSdkClient<T>(
  discovery: ValidatedDiscovery,
  SDKClient: new (options: TrustedClientOptions) => T,
  allowInsecureLocalDevelopment = false,
): T {
  const policy = {allowInsecureLocalDevelopment};
  validateOrigin(discovery.origin, policy);
  const api = new URL(discovery.apiPublic);
  const gateway = new URL(discovery.gateway);
  const local = api.hostname === '127.0.0.1' && gateway.hostname === '127.0.0.1';
  if (api.username || api.password || api.hash || api.search ||
      gateway.username || gateway.password || gateway.hash || gateway.search ||
      (api.protocol !== 'https:' && !(allowInsecureLocalDevelopment &&
        local && api.protocol === 'http:')) ||
      (gateway.protocol !== 'wss:' && !(allowInsecureLocalDevelopment &&
        local && gateway.protocol === 'ws:'))) {
    throw new Error('untrusted Fluxer SDK endpoint');
  }
  // A partial ClientOptions.instance silently inherits hosted Fluxer defaults
  // from v3.3.0's resolveInstanceEndpoints(). Refuse incomplete discovery
  // instead of letting this self-hosted client retain first-party endpoints.
  const instance: TrustedClientOptions['instance'] = {
    api_public: discovery.apiPublic,
    gateway: discovery.gateway,
  };
  const requiredEndpoints = [
    'api', 'api_client', 'media', 'static_cdn', 'marketing', 'admin',
    'invite', 'gift', 'webapp',
  ] as const;
  for (const name of requiredEndpoints) {
    const value = discovery.endpoints[name];
    if (typeof value !== 'string' || !value.trim()) {
      throw new Error('missing Fluxer SDK discovery endpoint');
    }
    instance[name] = value;
  }
  // SDK v3.3.0 accepts ClientOptions.WebSocket and uses it for every shard
  // connection/reconnect, including a Gateway URL returned by /gateway/bot.
  class TrustedWebSocket extends WebSocket {
    constructor(url: string) {
      validateGatewayConnection(url, discovery.gateway);
      super(url);
    }
  }
  return new SDKClient({
    instance,
    // SDK v3.3.0 defaults non-idempotent REST retries to zero already. Keep
    // its retry budget at zero until the transport write policy is verified.
    rest: {retries: 0},
    WebSocket: TrustedWebSocket,
  });
}
