/** Own the one authenticated SDK session, Gateway ingress and private RPC port.
 *
 * Construction requires a caller-supplied *egress-vetted* SDK factory and
 * bot operation handler. No unverified Client.fromDiscovery connection is
 * opened implicitly: that production security boundary remains fail-closed.
 */
import {SdkSession, type SdkClient, type SdkSessionConfig} from './sdk_session.ts';
import {FluxerSdkGatewayHandler} from './gateway.ts';
import {GatewayEventForwarder, type EventDeliveryConfig} from './event_delivery.ts';
import {PrivateRpcService, PrivateRpcError, RPC_WRITES, type RpcHandler, type RpcOperation} from './transport.ts';

export type ServiceConfig = Omit<SdkSessionConfig, 'createClient'> &
  Omit<EventDeliveryConfig, 'connectorKey'> & {
    connectorKey: string;
    rpcPort?: number;
    createVettedClient: () => Promise<SdkClient>;
    execute: RpcHandler;
  };

/** One independently closable Fluxer service; no second SDK client or sender. */
export class FluxerNodeService {
  private readonly session: SdkSession;
  private readonly forwarder: GatewayEventForwarder;
  private readonly gateway: FluxerSdkGatewayHandler;
  private readonly rpc: PrivateRpcService;
  private currentClient: SdkClient | undefined;
  private starting: Promise<number> | undefined;
  private closing: Promise<void> | undefined;
  private gatewayFault = false;
  private gatewayContextUnverified = false;
  private shutdownRequested = false;
  private readonly instanceOrigin: string;

  constructor(config: ServiceConfig) {
    // Session-origin attestation prevents a sidecar configured for another
    // trusted instance from inheriting this connector's physical writes.
    this.instanceOrigin = new URL(config.origin).origin;
    // All private transports share the same exact immutable connector identity.
    this.forwarder = new GatewayEventForwarder(config);
    this.gateway = new FluxerSdkGatewayHandler({connectorKey: config.connectorKey,
      forwarder: this.forwarder,
      botId: () => this.currentClient?.user?.id as string | undefined,
      onProblem: (reason) => {
        if (reason === 'context_unverified') {
          // An unverified inbound forum does not make outbound SDK REST unsafe.
          this.gatewayContextUnverified = true;
        } else {
          this.gatewayFault = true;
        }
      }});
    this.session = new SdkSession({...config,
      createClient: async () => {
        const client = await config.createVettedClient();
        this.currentClient = client;
        // SDK listeners must be installed before login() can emit events.
        this.gateway.attach(client);
        return client;
      }});
    const dispatch: RpcHandler = async (operation: RpcOperation, args) => {
      const status = this.session.health();
      const identityMatches = this.liveIdentityMatches(status.bot_id);
      if (operation === 'health') {
        // A cached successful login is not proof that the SDK still represents
        // that bot. Health consumers must not admit a drifted identity as READY.
        return {...status,
          state: status.state === 'ready' && !identityMatches ? 'degraded' : status.state,
          instance_origin: this.instanceOrigin,
          gateway_gap: status.gateway_gap || this.gatewayFault,
          gateway_context_unverified: this.gatewayContextUnverified};
      }
      if (this.shutdownRequested || status.state !== 'ready' || this.gatewayFault) {
        throw new PrivateRpcError('unavailable', 503);
      }
      // Enforce readiness at the SDK-owning admission boundary, not just at
      // Python's earlier health check. There is no await between proof and the
      // executor call, so a reconnect or SDK user swap cannot race in this turn.
      if (RPC_WRITES.includes(operation as (typeof RPC_WRITES)[number]) &&
          (status.gateway_gap || !identityMatches)) {
        throw new PrivateRpcError('unavailable', 503);
      }
      return config.execute(operation, args);
    };
    this.rpc = new PrivateRpcService({connectorKey: config.connectorKey,
      transportSecret: config.transportSecret, port: config.rpcPort ?? 0,
      handler: dispatch});
  }

  /** Compare the current SDK user with the bot identity verified at login. */
  private liveIdentityMatches(attestedBotId: string | undefined): boolean {
    const user = this.currentClient?.user;
    return typeof attestedBotId === 'string' && attestedBotId.length > 0 &&
      user?.bot === true && user.id === attestedBotId;
  }

  /** Start SDK first; only an authenticated READY session exposes private RPC. */
  start(): Promise<number> {
    if (this.shutdownRequested || this.starting) throw new Error('Fluxer service already started');
    this.starting = (async () => {
      try {
        await this.session.start();
        return await this.rpc.start();
      } catch {
        await this.close().catch(() => {});
        throw new Error('Fluxer service initialization refused');
      }
    })();
    return this.starting;
  }

  /** Join callbacks, private RPC writes and SDK I/O before final client destroy. */
  close(): Promise<void> {
    if (this.closing) return this.closing;
    this.shutdownRequested = true;
    this.closing = (async () => {
      // Drain in dependency order. If teardown fails, surface it rather than
      // claiming that the connector can be safely replaced by another sender.
      let failure = false;
      try {await this.gateway.close();} catch {failure = true;}
      try {await this.rpc.close();} catch {failure = true;}
      // Even when the RPC listener fails to close, an SDK socket must not
      // stay active after the service reports a failed teardown attempt.
      try {await this.session.close();} catch {failure = true;}
      if (failure) throw new Error('Fluxer service teardown incomplete');
    })();
    return this.closing;
  }
}
