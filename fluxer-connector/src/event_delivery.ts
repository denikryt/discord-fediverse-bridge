/** Authenticated, bounded Node → Python normalized Fluxer Gateway ingress.
 *
 * An HTTP ACK means Python has finished admission to its one Fluxer sink; it
 * does NOT mean ActivityPub publication or a durable Gateway replay cursor.
 * Unknown ACKs are surfaced but never replayed without the later canonical
 * provenance/dedup proof required by issue #13.
 */
import type {NeutralWire} from './normalization.ts';

export type EventDeliveryConfig = {
  connectorKey: string;
  pythonIngressUrl: string;
  transportSecret: string;
  allowedInternalHosts?: readonly string[];
  maxInFlight?: number;
  timeoutMs?: number;
};

const MAX_EVENT_BYTES = 256 * 1024;
const MAX_ACK_BYTES = 4096;
const TYPES = new Set(['ThreadCreated', 'ThreadUpdated', 'ThreadDeleted',
  'MessageCreated', 'MessageUpdated', 'MessageDeleted']);

/** A safe error reason, never provider JSON or an authorization header. */
export class GatewayEventDeliveryError extends Error {
  constructor(message: string) {super(message);}
}

function vettedUrl(input: string, allowed: readonly string[]): string {
  /** Non-public HTTP egress with no credentials, redirect targets or query. */
  const url = new URL(input);
  const loopback = ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname);
  if (!url.port || url.username || url.password || url.hash || url.search ||
      url.pathname !== '/internal/fluxer/events' ||
      (url.protocol !== 'http:' && url.protocol !== 'https:') ||
      (!loopback && !allowed.includes(url.hostname)) ||
      (!loopback && url.protocol !== 'https:')) {
    throw new Error('invalid Fluxer private Python ingress address');
  }
  return url.toString();
}

async function boundedAck(response: Response): Promise<unknown> {
  /** The response may be untrusted and must never consume unlimited memory. */
  if (!response.body) throw new Error('missing event ingress response');
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > MAX_ACK_BYTES) throw new Error('oversized event ingress response');
      chunks.push(value);
    }
  } finally {reader.releaseLock();}
  const body = Buffer.concat(chunks.map(chunk => Buffer.from(chunk)));
  return JSON.parse(body.toString('utf8'));
}

/** One connector-owned ingress admission and HTTP task-drain lifecycle. */
export class GatewayEventForwarder {
  private readonly url: string;
  private readonly connector: string;
  private readonly secret: string;
  private readonly max: number;
  private readonly timeout: number;
  private readonly active = new Set<Promise<void>>();
  private open = true;
  private stopping: Promise<void> | undefined;

  constructor(config: EventDeliveryConfig) {
    this.url = vettedUrl(config.pythonIngressUrl, config.allowedInternalHosts ?? []);
    if (typeof config.connectorKey !== 'string' || !config.connectorKey ||
        typeof config.transportSecret !== 'string' || config.transportSecret.length < 8 ||
        !Number.isInteger(config.maxInFlight ?? 8) ||
        (config.maxInFlight ?? 8) < 1 || (config.maxInFlight ?? 8) > 8 ||
        !Number.isInteger(config.timeoutMs ?? 10_000) ||
        (config.timeoutMs ?? 10_000) < 1 || (config.timeoutMs ?? 10_000) > 60_000) {
      throw new Error('invalid Fluxer Gateway forwarding configuration');
    }
    this.connector = config.connectorKey;
    this.secret = config.transportSecret;
    this.max = config.maxInFlight ?? 8;
    this.timeout = config.timeoutMs ?? 10_000;
  }

  /** Admit one bounded normalized DTO; completion is only an ingress ACK. */
  forward(event: NeutralWire): Promise<void> {
    if (!this.open) return Promise.reject(new GatewayEventDeliveryError('Fluxer ingress quiescing'));
    if (typeof event !== 'object' || event === null ||
        event.connector_key !== this.connector || !TYPES.has(event.type)) {
      return Promise.reject(new GatewayEventDeliveryError('Fluxer connector mismatch or event type'));
    }
    let body: string;
    try {body = JSON.stringify(event);}
    catch {return Promise.reject(new GatewayEventDeliveryError('invalid Fluxer Gateway wire DTO'));}
    if (Buffer.byteLength(body) > MAX_EVENT_BYTES) {
      return Promise.reject(new GatewayEventDeliveryError('Fluxer Gateway event too large'));
    }
    if (this.active.size >= this.max) {
      return Promise.reject(new GatewayEventDeliveryError('Fluxer ingress capacity exhausted'));
    }
    const attempt = this.send(body);
    // Register before returning the promise, so close() observes this attempt.
    this.active.add(attempt);
    void attempt.then(() => this.active.delete(attempt), () => this.active.delete(attempt));
    return attempt;
  }

  private async send(body: string): Promise<void> {
    let response: Response;
    try {
      response = await fetch(this.url, {
        method: 'POST', redirect: 'manual',
        headers: {
          'authorization': `Bearer ${this.secret}`,
          'x-fluxer-connector': this.connector,
          'content-type': 'application/json', 'accept': 'application/json',
        },
        body, signal: AbortSignal.timeout(this.timeout),
      });
    } catch {
      // The remote sink might have completed before a response was lost.
      throw new GatewayEventDeliveryError('Fluxer event ingress ACK unknown');
    }
    if (response.status !== 200) {
      await response.body?.cancel();
      if ([400, 401, 403, 404, 413, 422, 429, 503].includes(response.status) ||
          (response.status >= 300 && response.status < 400)) {
        throw new GatewayEventDeliveryError('Fluxer event ingress refused');
      }
      throw new GatewayEventDeliveryError('Fluxer event ingress ACK unknown');
    }
    try {
      const answer = await boundedAck(response);
      if (!answer || typeof answer !== 'object' ||
          (answer as {accepted?: unknown}).accepted !== true) {
        throw new Error('Fluxer ingress did not acknowledge admission');
      }
    } catch {
      throw new GatewayEventDeliveryError('Fluxer event ingress ACK unknown');
    }
  }

  /** Close admission first; join every earlier HTTP call without losing a task. */
  close(): Promise<void> {
    if (this.stopping) return this.stopping;
    this.open = false;
    this.stopping = (async () => {
      if (this.active.size) await Promise.allSettled([...this.active]);
    })();
    return this.stopping;
  }
}
