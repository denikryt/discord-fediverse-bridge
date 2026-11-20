/** Own one Fluxer SDK client from authenticated login through orderly teardown.
 *
 * Only a vetted SDK client factory may supply the transport. This component
 * deliberately does not open unaudited REST/Gateway sockets: discovery DNS
 * checks alone do not secure SDK-created network connections. The concrete
 * SDK egress policy and client factory are a separate runtime-integration step.
 */
import {validateOrigin} from './discovery.ts';

/** Minimal methods exercised by a Fluxer SDK session, independent of cache models. */
export type SdkClient = {
  on(event: string, listener: (...args: unknown[]) => void): unknown;
  off(event: string, listener: (...args: unknown[]) => void): unknown;
  login(token: string): Promise<unknown>;
  destroy(): Promise<unknown> | unknown;
  user?: {id?: unknown; bot?: unknown} | null;
  instance?: {endpoints?: {api_public?: unknown}} | null;
  rest?: {baseUrl?: unknown} | null;
};

/** The provider supplies a socket-policy-vetted client for exactly this origin. */
export type SdkSessionConfig = {
  origin: string;
  apiPublic: string;
  botToken: string;
  allowInsecureLocalDevelopment?: boolean;
  connectorKey?: string;
  readyTimeoutMs?: number;
  createClient: () => Promise<SdkClient>;
};

export type SdkHealth = {
  state: 'new' | 'connecting' | 'ready' | 'degraded' | 'stopping' | 'stopped';
  connector_key: string | undefined;
  bot_id?: string;
  gateway_gap: boolean;
};

function normalizedEndpoint(input: unknown, allowLocalHttp = false): string {
  /** Compare exact canonical bot API endpoints, never the first-party API. */
  if (typeof input !== 'string') throw new Error('bot API endpoint missing');
  const endpoint = new URL(input);
  if (endpoint.username || endpoint.password || endpoint.hash || endpoint.search ||
      (endpoint.protocol !== 'https:' &&
        !(allowLocalHttp && endpoint.protocol === 'http:' &&
          ['localhost', '127.0.0.1', '[::1]'].includes(endpoint.hostname)))) {
    throw new Error('invalid bot API endpoint');
  }
  return endpoint.href;
}

/** One listener set and one client handle: no SDK login outside this owner. */
export class SdkSession {
  private readonly config: SdkSessionConfig;
  private readonly expectedApi: string;
  private readonly timeout: number;
  private client: SdkClient | undefined;
  private readonly listeners: Array<[string, (...args: unknown[]) => void]> = [];
  private state: SdkHealth['state'] = 'new';
  private gap = false;
  private botId: string | undefined;
  private readyReceived = false;
  private authenticated = false;
  private previouslyReady = false;
  private resumePending = false;
  private readySignal: (() => void) | undefined;
  private stopping = false;
  private starting: Promise<void> | undefined;
  private closing: Promise<void> | undefined;
  private disposed = false;
  private disposing: Promise<void> | undefined;

  constructor(config: SdkSessionConfig) {
    /** Reject malformed operator configuration before the factory is entered. */
    validateOrigin(config.origin, {
      allowInsecureLocalDevelopment: config.allowInsecureLocalDevelopment === true,
    });
    if (!config.botToken || config.botToken !== config.botToken.trim() ||
        typeof config.createClient !== 'function' ||
        !Number.isInteger(config.readyTimeoutMs ?? 10_000) ||
        (config.readyTimeoutMs ?? 10_000) < 1 ||
        (config.readyTimeoutMs ?? 10_000) > 60_000) {
      throw new Error('invalid Fluxer SDK configuration');
    }
    this.expectedApi = normalizedEndpoint(config.apiPublic,
      config.allowInsecureLocalDevelopment === true);
    this.timeout = config.readyTimeoutMs ?? 10_000;
    this.config = config;
  }

  /** Return redacted operational status; neither tokens nor endpoint data leak. */
  health(): SdkHealth {
    return {state: this.state, connector_key: this.config.connectorKey,
      ...(this.botId ? {bot_id: this.botId} : {}), gateway_gap: this.gap};
  }

  /** Connect once; mark ready only after both SDK READY and verified bot login. */
  start(): Promise<void> {
    if (this.starting || this.state !== 'new') {
      throw new Error('Fluxer SDK session already started');
    }
    this.state = 'connecting';
    this.starting = this.initialize();
    return this.starting;
  }

  private observe(event: string, listener: (...args: unknown[]) => void): void {
    /** Only listeners installed by this owner may be removed by its shutdown. */
    this.client!.on(event, listener);
    this.listeners.push([event, listener]);
  }

  private async awaitReady(): Promise<void> {
    /** READY can race ahead of login() resolution; observe it in both orders. */
    if (this.readyReceived) return;
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.readySignal = undefined;
        reject(new Error('Fluxer READY timeout'));
      }, this.timeout);
      this.readySignal = () => {clearTimeout(timer); this.readySignal = undefined; resolve();};
      if (this.readyReceived) this.readySignal();
    });
  }

  private async initialize(): Promise<void> {
    try {
      const client = await this.config.createClient();
      if (this.stopping) {
        // Factory completion after close still transfers ownership to teardown.
        this.client = client;
        throw new Error('Fluxer SDK startup cancelled');
      }
      this.client = client;
      if (normalizedEndpoint(client.instance?.endpoints?.api_public,
        this.config.allowInsecureLocalDevelopment === true) !== this.expectedApi) {
        throw new Error('Fluxer SDK endpoint mismatch');
      }
      // The configured instance metadata alone does not prove where REST sends
      // bearer requests. Inspect the SDK's actual base URL if it exposes one.
      if (typeof client.rest?.baseUrl === 'string') {
        const rest = new URL(client.rest.baseUrl);
        const api = new URL(this.expectedApi);
        const basePath = api.pathname.replace(/\/$/, '');
        const path = rest.pathname.replace(/\/$/, '');
        if (rest.origin !== api.origin ||
            (path !== basePath && path !== `${basePath}/v1`)) {
          throw new Error('Fluxer SDK REST endpoint mismatch');
        }
      }
      // SDK v3.3.0 public ClientEvents expose shard lifecycle, not the raw
      // Gateway `disconnect`/`invalidSession` opcodes. READY alone is not
      // proof that a lost session was resumed with complete event history.
      const ready = (newSession: boolean): void => {
        this.readyReceived = true;
        if (newSession && this.previouslyReady && this.resumePending) this.gap = true;
        this.resumePending = false;
        this.readySignal?.();
        if (this.authenticated && !this.stopping) this.state = 'ready';
        this.previouslyReady = true;
      };
      const degraded = (): void => {
        this.readyReceived = false;
        if (this.previouslyReady) this.resumePending = true;
        if (!this.stopping) this.state = 'degraded';
      };
      this.observe('ready', () => ready(true));
      this.observe('shardReady', () => {
        // A fresh shard READY after loss does not guarantee event replay.
        if (this.previouslyReady) ready(true);
      });
      this.observe('shardResumed', () => ready(false));
      this.observe('resumed', () => ready(false));
      this.observe('shardDisconnect', degraded);
      this.observe('shardReconnecting', degraded);
      this.observe('shardError', degraded);
      this.observe('error', degraded);
      // Endpoint identity is verified before passing the bot credential to SDK.
      await client.login(this.config.botToken);
      await this.awaitReady();
      if (this.stopping || client.user?.bot !== true ||
          typeof client.user.id !== 'string' || !client.user.id) {
        throw new Error('Fluxer SDK identity unavailable');
      }
      this.botId = client.user.id;
      this.authenticated = true;
      this.state = 'ready';
      this.previouslyReady = true;
    } catch {
      // Provider exceptions may contain bearer tokens, paths or message bodies.
      let shutdownConfirmed = true;
      try {await this.dispose();}
      catch {shutdownConfirmed = false;}
      // A failed SDK destroy cannot be reported as a completed shutdown.
      this.state = shutdownConfirmed ? 'stopped' : 'degraded';
      throw new Error('Fluxer SDK initialization refused');
    }
  }

  private async dispose(): Promise<void> {
    /** Do not treat a failed SDK destroy as proof that Gateway I/O stopped. */
    if (this.disposed) return;
    if (this.disposing) return this.disposing;
    const client = this.client;
    if (!client) return;
    this.disposing = (async () => {
      await client.destroy();
      for (const [event, listener] of this.listeners) client.off(event, listener);
      this.listeners.length = 0;
      this.disposed = true;
    })();
    try {await this.disposing;}
    catch {
      this.disposing = undefined;
      throw new Error('Fluxer SDK teardown incomplete');
    }
  }

  /** Prevent new readiness and await the startup owner before SDK teardown. */
  close(): Promise<void> {
    if (this.closing) return this.closing;
    this.stopping = true;
    this.state = 'stopping';
    this.closing = (async () => {
      if (this.starting) await this.starting.catch(() => {});
      try {await this.dispose(); this.state = 'stopped';}
      catch {this.state = 'degraded'; throw new Error('Fluxer SDK teardown incomplete');}
    })();
    // An unsuccessful close can be retried without claiming a stopped SDK.
    void this.closing.catch(() => {this.closing = undefined;});
    return this.closing;
  }
}
