/** Private authenticated RPC ingress, independent of Fluxer SDK wire routes.
 *
 * This module never parses arbitrary platform operation names or tokens from
 * caller-provided URLs. The SDK owner will supply an explicit method dispatcher.
 */
import { createServer, type Server, type IncomingMessage, type ServerResponse } from 'node:http';
import { timingSafeEqual } from 'node:crypto';
import type { AddressInfo } from 'node:net';

export const RPC_READS = [
  'fetch_channel', 'fetch_thread', 'fetch_message', 'check_permissions',
  'capabilities', 'reconcile_create', 'health',
] as const;
export const RPC_WRITES = [
  'create_forum_thread', 'create_reply_thread', 'send_message', 'edit_message',
  'delete_message',
] as const;
export type RpcOperation = (typeof RPC_READS)[number] | (typeof RPC_WRITES)[number];
const OPERATIONS = new Set<string>([...RPC_READS, ...RPC_WRITES]);
const MAX_BYTES = 256 * 1024;

/** Validate the HTTP Authorization bearer identically for main and direct RPC owners. */
export function validPrivateBearer(secret: unknown): secret is string {
  // Whitespace, non-ASCII and control bytes are unsafe in a header value.
  return typeof secret === 'string' && /^[\x21-\x7e]{8,}$/.test(secret);
}

export class PrivateRpcError extends Error {
  /** Only transport-neutral, deliberate refusal reasons cross the process boundary. */
  readonly reason: 'forbidden' | 'not_found' | 'unsupported' | 'invalid_request' |
    'rate_limited' | 'unavailable' | 'unknown_delivery';
  readonly status: number;
  constructor(reason: PrivateRpcError['reason'], status = 422) {
    super('Fluxer RPC operation refused');
    this.reason = reason;
    this.status = status;
  }
}

export type RpcHandler = (operation: RpcOperation, args: Readonly<Record<string, unknown>>)
  => Promise<Record<string, unknown>>;
export type RpcServiceConfig = {
  connectorKey: string;
  transportSecret: string;
  handler: RpcHandler;
  host?: '127.0.0.1' | '::1';
  port?: number;
  maxInFlight?: number;
};

function equalSecret(input: string, expected: string): boolean {
  /** Constant-time comparison when token byte lengths agree. */
  const left = Buffer.from(input);
  const right = Buffer.from(expected);
  return left.length === right.length && timingSafeEqual(left, right);
}

function isRecord(input: unknown): input is Record<string, unknown> {
  return input !== null && typeof input === 'object' && !Array.isArray(input);
}

export class PrivateRpcService {
  /** The only Node sidecar RPC admission/drain owner. */
  private readonly server: Server;
  private readonly work = new Set<Promise<void>>();
  private open = true;
  private closing: Promise<void> | undefined;
  private listening = false;
  private listenAttempt: Promise<number> | undefined;
  private readonly key: string;
  private readonly secret: string;
  private readonly handler: RpcHandler;
  private readonly maxInFlight: number;
  private readonly host: string;
  private readonly port: number;

  constructor(config: RpcServiceConfig) {
    if (!config.connectorKey || !validPrivateBearer(config.transportSecret) ||
        !Number.isInteger(config.port ?? 0) || (config.port ?? 0) < 0 ||
        (config.port ?? 0) > 65535 || !Number.isInteger(config.maxInFlight ?? 8) ||
        (config.maxInFlight ?? 8) < 1 || (config.maxInFlight ?? 8) > 8) {
      throw new Error('invalid Fluxer private RPC configuration');
    }
    this.key = config.connectorKey;
    this.secret = `Bearer ${config.transportSecret}`;
    this.handler = config.handler;
    this.maxInFlight = config.maxInFlight ?? 8;
    this.host = config.host ?? '127.0.0.1';
    this.port = config.port ?? 0;
    this.server = createServer((req, res) => {
      // Register an admitted request before any await or async body parsing.
      const operation = this.handle(req, res);
      this.work.add(operation);
      void operation.then(() => this.work.delete(operation), () => this.work.delete(operation));
    });
  }

  start(): Promise<number> {
    /** Track listen admission so a concurrent close cannot leak a late bind. */
    if (!this.open || this.listenAttempt) {
      return Promise.reject(new Error('Fluxer private RPC already started or closed'));
    }
    this.listenAttempt = new Promise<number>((resolve, reject) => {
      const failed = (error: Error): void => {
        this.server.removeListener('error', failed);
        reject(error);
      };
      this.server.once('error', failed);
      this.server.listen(this.port, this.host, () => {
        this.server.removeListener('error', failed);
        this.listening = true;
        const address = this.server.address();
        if (!address || typeof address === 'string') {
          reject(new Error('invalid private RPC address'));
        } else {
          resolve((address as AddressInfo).port);
        }
      });
    });
    return this.listenAttempt;
  }

  private respond(res: ServerResponse, status: number, body: Record<string, unknown>): void {
    /** Never return provider exception strings, SDK errors or credentials. */
    if (res.headersSent || res.destroyed) return;
    res.writeHead(status, {'content-type': 'application/json', 'cache-control': 'no-store'});
    res.end(JSON.stringify({connector_key: this.key, ...body}));
  }

  private reject(res: ServerResponse, status: number, reason: string): void {
    this.respond(res, status, {ok: false, error: {reason}});
  }

  private async handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    /** Authenticate before reading any body; validate connector and action next. */
    if (!this.open) { this.reject(res, 503, 'unavailable'); return; }
    if (req.method !== 'POST' || req.url !== '/rpc') {
      this.reject(res, 404, 'not_found'); return;
    }
    if (typeof req.headers.authorization !== 'string' ||
        !equalSecret(req.headers.authorization, this.secret)) {
      this.reject(res, 403, 'forbidden'); return;
    }
    if (this.work.size > this.maxInFlight) {
      this.reject(res, 429, 'rate_limited'); return;
    }
    const declared = Number(req.headers['content-length']);
    if (!Number.isNaN(declared) && declared > MAX_BYTES) {
      this.reject(res, 413, 'invalid_request'); return;
    }
    const chunks: Buffer[] = [];
    let length = 0;
    try {
      for await (const chunk of req) {
        length += chunk.length;
        if (length > MAX_BYTES) {
          this.reject(res, 413, 'invalid_request'); return;
        }
        chunks.push(chunk);
      }
      let request: unknown;
      try {
        request = JSON.parse(Buffer.concat(chunks).toString('utf-8'));
      } catch {
        this.reject(res, 422, 'invalid_request'); return;
      }
      if (!isRecord(request) || request.connector_key !== this.key ||
          typeof request.operation !== 'string' || !OPERATIONS.has(request.operation) ||
          !isRecord(request.args) || Object.keys(request).some(k =>
            !['connector_key', 'operation', 'args'].includes(k))) {
        this.reject(res, 422, 'invalid_request'); return;
      }
      const answer = await this.handler(request.operation as RpcOperation, request.args);
      if (!isRecord(answer)) throw new PrivateRpcError('invalid_request');
      this.respond(res, 200, {ok: true, result: answer});
    } catch (err) {
      if (err instanceof PrivateRpcError) {
        this.reject(res, err.status, err.reason);
      } else {
        this.reject(res, 503, 'unavailable');
      }
    }
  }

  close(): Promise<void> {
    /** Refuse new RPCs and join a possibly in-progress TCP listen first. */
    if (this.closing) return this.closing;
    this.open = false;
    this.closing = (async () => {
      if (this.listenAttempt) await this.listenAttempt.catch(() => {});
      if (this.listening) {
        const closed = new Promise<void>((resolve, reject) =>
          this.server.close(err => err ? reject(err) : resolve()));
        while (this.work.size) await Promise.allSettled([...this.work]);
        this.server.closeIdleConnections();
        await closed;
        this.listening = false;
      }
    })();
    return this.closing;
  }
}
