/** Bind public @fluxerjs/core ClientEvents to one Node normalizer and HTTP sink.
 *
 * Register before client.login. This is the only SDK event subscription owner;
 * it is not a second Gateway/WebSocket client or a canonical publisher.
 */
import {normalizeSdkEvent, type SdkEventName, type Observation} from './normalization.ts';
import type {GatewayEventForwarder} from './event_delivery.ts';

/** Public @fluxerjs/core 3.3.0 Events keys, checked against tag source. */
export const PUBLIC_SDK_EVENT_NAMES = {
  threadCreate: 'threadCreate', threadUpdate: 'threadUpdate', threadDelete: 'threadDelete',
  messageCreate: 'messageCreate', messageUpdate: 'messageUpdate', messageDelete: 'messageDelete',
} as const;
export type SdkEventSource = {
  on(event: string, listener: (...args: unknown[]) => void): unknown;
  off(event: string, listener: (...args: unknown[]) => void): unknown;
};
export type GatewayHandlerOptions = {
  connectorKey: string;
  forwarder: Pick<GatewayEventForwarder, 'forward' | 'close'>;
  botId: () => string | undefined;
  onProblem: (reason: 'normalization_failed' | 'admission_failed') => void;
  maxPending?: number;
};

/** One owned listener set and bounded in-flight forwarding task set. */
export class FluxerSdkGatewayHandler {
  private source: SdkEventSource | undefined;
  private readonly listeners: Array<[string, (...args: unknown[]) => void]> = [];
  private readonly work = new Set<Promise<void>>();
  private open = true;
  private closing: Promise<void> | undefined;
  private readonly max: number;

  private readonly config: GatewayHandlerOptions;

  constructor(config: GatewayHandlerOptions) {
    this.config = config;
    if (!config.connectorKey || !Number.isInteger(config.maxPending ?? 8) ||
        (config.maxPending ?? 8) < 1 || (config.maxPending ?? 8) > 32) {
      throw new Error('invalid Fluxer Gateway listener configuration');
    }
    this.max = config.maxPending ?? 8;
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
    const task = this.config.forwarder.forward(wire).catch(() => {
      // Unknown HTTP acknowledgement cannot be treated as durable event replay.
      this.config.onProblem('admission_failed');
    });
    this.work.add(task);
    void task.finally(() => {this.work.delete(task);});
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
