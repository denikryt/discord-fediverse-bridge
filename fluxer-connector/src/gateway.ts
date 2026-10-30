/** Bind public @fluxerjs/core ClientEvents to one Node normalizer and HTTP sink.
 *
 * Register before client.login. This is the only SDK event subscription owner;
 * it is not a second Gateway/WebSocket client or a canonical publisher.
 */
import {normalizeSdkEvent, type SdkEventName, type Observation, type NeutralWire} from './normalization.ts';
import type {GatewayEventForwarder} from './event_delivery.ts';
import {createSdkReadHandler, type SdkReadClient} from './rest_reads.ts';

/** Public @fluxerjs/core 3.3.0 Events keys, checked against tag source. */
export const PUBLIC_SDK_EVENT_NAMES = {
  threadCreate: 'threadCreate', threadUpdate: 'threadUpdate', threadDelete: 'threadDelete',
  messageCreate: 'messageCreate', messageUpdate: 'messageUpdate', messageDelete: 'messageDelete',
} as const;
export type SdkEventSource = {
  on(event: string, listener: (...args: unknown[]) => void): unknown;
  off(event: string, listener: (...args: unknown[]) => void): unknown;
  channels?: SdkReadClient['channels'];
};
export type GatewayHandlerOptions = {
  connectorKey: string;
  forwarder: Pick<GatewayEventForwarder, 'forward' | 'close'>;
  botId: () => string | undefined;
  onProblem: (reason: 'normalization_failed' | 'admission_failed' | 'context_unverified') => void;
  maxPending?: number;
  contextLookupTimeoutMs?: number;
};

/** One owned listener set and bounded in-flight forwarding task set. */
export class FluxerSdkGatewayHandler {
  private source: SdkEventSource | undefined;
  private readonly listeners: Array<[string, (...args: unknown[]) => void]> = [];
  private readonly work = new Set<Promise<void>>();
  private open = true;
  private closing: Promise<void> | undefined;
  private readonly max: number;
  private readonly lookupTimeout: number;

  private readonly config: GatewayHandlerOptions;

  constructor(config: GatewayHandlerOptions) {
    this.config = config;
    if (!config.connectorKey || !Number.isInteger(config.maxPending ?? 8) ||
        (config.maxPending ?? 8) < 1 || (config.maxPending ?? 8) > 32 ||
        !Number.isInteger(config.contextLookupTimeoutMs ?? 10_000) ||
        (config.contextLookupTimeoutMs ?? 10_000) < 1 ||
        (config.contextLookupTimeoutMs ?? 10_000) > 60_000) {
      throw new Error('invalid Fluxer Gateway listener configuration');
    }
    this.max = config.maxPending ?? 8;
    this.lookupTimeout = config.contextLookupTimeoutMs ?? 10_000;
  }

  /** Attach once, before SDK.login() can emit any public events. */
  attach(source: SdkEventSource): void {
    if (!this.open || this.source) throw new Error('Fluxer Gateway listeners unavailable');
    this.source = source;
    for (const [kind, sdkName] of Object.entries(PUBLIC_SDK_EVENT_NAMES)) {
      const listen = (...args: unknown[]): void => this.accept(kind as SdkEventName, args);
      source.on(sdkName, listen);
      this.listeners.push([sdkName, listen]);
    }
  }

  private accept(kind: SdkEventName, args: readonly unknown[]): void {
    /** Overload must fail closed, not create untracked fire-and-forget requests. */
    if (!this.open) return;
    // Keep one task per event until its SDK read AND HTTP delivery settle.
    if (this.work.size >= this.max) {
      this.config.onProblem('admission_failed');
      return;
    }
    let wire;
    try {
      const observation: Observation = {ownedBotId: this.config.botId()};
      wire = normalizeSdkEvent(this.config.connectorKey, kind, args, observation);
    } catch {
      this.config.onProblem('normalization_failed');
      return;
    }
    if (!wire) return;
    // Admission is owned synchronously, before the first SDK metadata await.
    const task = this.forward(kind, wire);
    this.work.add(task);
    void task.then(() => this.work.delete(task), () => this.work.delete(task));
  }

  /** Enrich a creation with *live* forum membership before Python admission.
   *
   * SDK Message.channelId is the immediate thread, never proof of its parent.
   * Reuse CP021's force-fetch and exact parent/guild checks rather than cache
   * names or a second native-event normalizer. An unverified observation must
   * still reach the Fluxer sink with no claimable parent metadata.
   */
  private async forward(kind: SdkEventName, wire: NeutralWire): Promise<void> {
    let pendingRead: Promise<unknown> | undefined;
    if (kind === 'messageCreate') {
      const channels = this.source?.channels;
      if (channels) {
        let timer: ReturnType<typeof setTimeout> | undefined;
        try {
          const read = createSdkReadHandler({channels});
          const containerId = (wire.message as {external_container_id: string}).external_container_id;
          // SDK reads are bounded by SDK transport timeout/retry rules. This
          // shorter deadline bounds event forwarding, not the remote socket.
          pendingRead = read('fetch_thread', {id: containerId});
          const result = await Promise.race([
            pendingRead,
            new Promise<never>((_, reject) => {
              timer = setTimeout(() => reject(new Error('context lookup expired')),
                this.lookupTimeout);
            }),
          ]);
          const thread = result as {id: string; parent_id: string; guild_id: string; kind: string};
          if (thread.kind !== 'forum' || thread.id !== containerId ||
              (wire.external_guild_id != null && wire.external_guild_id !== thread.guild_id)) {
            throw new Error('unverified thread/forum/guild context');
          }
          const parent = {connector_key: this.config.connectorKey,
            external_channel_id: thread.parent_id, kind: 'forum',
            external_guild_id: thread.guild_id};
          wire.parent_channel = parent;
          wire.thread_context = {channel: parent, external_thread_id: containerId};
          wire.external_guild_id = thread.guild_id;
        } catch {
          // The durable forum-ownership gate is in Python/SQLite; a failed or
          // contradicting SDK lookup must never manufacture its input claim.
          this.config.onProblem('context_unverified');
        } finally {
          if (timer !== undefined) clearTimeout(timer);
        }
      } else {
        this.config.onProblem('context_unverified');
      }
    }
    try {
      await this.config.forwarder.forward(wire);
    } catch {
      // Unknown HTTP acknowledgement cannot be treated as durable event replay.
      this.config.onProblem('admission_failed');
    } finally {
      // A timed-out lookup is still SDK-owned I/O. Join it before releasing
      // this event's admission slot or declaring quiesce complete.
      if (pendingRead) await pendingRead.catch(() => {});
    }
  }

  /** Stop accepting new callbacks, unsubscribe and join admitted forwards. */
  close(): Promise<void> {
    if (this.closing) return this.closing;
    this.open = false;
    for (const [name, callback] of this.listeners) this.source?.off(name, callback);
    this.listeners.length = 0;
    this.closing = (async () => {
      if (this.work.size) await Promise.allSettled([...this.work]);
      await this.config.forwarder.close();
    })();
    return this.closing;
  }
}
