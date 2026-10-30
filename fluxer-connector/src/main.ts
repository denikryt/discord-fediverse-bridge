/** Standalone one-instance Fluxer SDK sidecar entry point.
 *
 * The operator supplies a vetted instance and a Node-only bot secret file.
 * The Python process receives only the independently configured IPC bearer;
 * writes remain unsupported until their real Fluxer REST contracts are proven.
 */
import {pathToFileURL} from 'node:url';
import {discoverInstance} from './discovery.ts';
import {readBotTokenFromFile} from './token_secret.ts';
import {createTrustedSdkClient} from './sdk_factory.ts';
import {FluxerNodeService} from './service.ts';
import {createSdkReadHandler} from './rest_reads.ts';
import {createSdkPermissionHandler} from './permissions.ts';
import {createSdkCapabilityHandler} from './capabilities.ts';
import {PrivateRpcError, type RpcHandler} from './transport.ts';

/** Read mandatory deployment values without echoing secret names or contents. */
function required(key: string): string {
  const value = process.env[key];
  if (!value || value.trim() !== value) {
    throw new Error('invalid Fluxer Node deployment configuration');
  }
  return value;
}

/** One operator-approved config, not an arbitrary JSON list of SDK clients. */
function readConfig(): {
  connectorKey: string; origin: string; botTokenFile: string; bearer: string;
  apiHost: string; gatewayHost: string; rpcPort: number;
  ingressUrl: string; allowInsecureLocalDevelopment: boolean;
} {
  const connectorKey = required('FLUXER_CONNECTOR_KEY');
  const origin = required('FLUXER_ORIGIN');
  const botTokenFile = required('FLUXER_BOT_TOKEN_FILE');
  const bearer = required('FLUXER_INTERNAL_BEARER');
  const apiHost = required('FLUXER_APPROVED_API_HOST');
  const gatewayHost = required('FLUXER_APPROVED_GATEWAY_HOST');
  const ingressUrl = required('FLUXER_PYTHON_INGRESS_URL');
  const requestedPort = required('FLUXER_RPC_PORT');
  const rpcPort = Number(requestedPort);
  if (!Number.isInteger(rpcPort) || rpcPort < 1 || rpcPort > 65535 ||
      connectorKey.length > 255 || bearer.length < 8 ||
      !/^[a-z0-9.-]+$/i.test(apiHost) || !/^[a-z0-9.-]+$/i.test(gatewayHost)) {
    throw new Error('invalid Fluxer Node deployment configuration');
  }
  const ingress = new URL(ingressUrl);
  if (ingress.protocol !== 'http:' || ingress.hostname !== '127.0.0.1' ||
      !ingress.port || ingress.username || ingress.password ||
      ingress.pathname !== '/internal/fluxer/events' || ingress.search || ingress.hash) {
    throw new Error('invalid Fluxer private Python ingress address');
  }
  return {connectorKey, origin, botTokenFile, bearer, apiHost, gatewayHost,
    rpcPort, ingressUrl, allowInsecureLocalDevelopment:
      process.env.FLUXER_ALLOW_INSECURE_LOCAL_DEVELOPMENT === 'true'};
}

/** Start one SDK session and join its owned RPC/Gateway work on shutdown. */
export async function runFluxerNodeService(): Promise<void> {
  const config = readConfig();
  // Discovery is tokenless and pinned to its validated destination. Do not
  // call Client.fromDiscovery() a second time after approving these endpoints.
  const discovery = await discoverInstance(config.origin, {
    allowInsecureLocalDevelopment: config.allowInsecureLocalDevelopment,
    allowedEndpointHosts: [config.apiHost, config.gatewayHost],
  });
  if (new URL(discovery.apiPublic).hostname !== config.apiHost ||
      new URL(discovery.gateway).hostname !== config.gatewayHost) {
    throw new Error('Fluxer operator-approved endpoint mismatch');
  }
  const token = await readBotTokenFromFile(config.botTokenFile);
  let handler: RpcHandler | undefined;
  const service = new FluxerNodeService({
    connectorKey: config.connectorKey,
    origin: config.origin,
    apiPublic: discovery.apiPublic,
    botToken: token,
    transportSecret: config.bearer,
    rpcPort: config.rpcPort,
    pythonIngressUrl: config.ingressUrl,
    allowInsecureLocalDevelopment: config.allowInsecureLocalDevelopment,
    createVettedClient: async () => {
      // The npm SDK is imported only after tokenless discovery and approved
      // hosts have passed; no fake SDK client is a production fallback.
      const {Client, Routes} = await import('@fluxerjs/core');
      const client = createTrustedSdkClient(discovery, Client,
        config.allowInsecureLocalDevelopment);
      const reads = createSdkReadHandler(client);
      const permissions = createSdkPermissionHandler(client);
      const capabilities = createSdkCapabilityHandler(client, Routes.channel);
      handler = (operation, args) => {
        if (operation === 'fetch_channel' || operation === 'fetch_thread' ||
            operation === 'fetch_message') return reads(operation, args);
        if (operation === 'check_permissions') return permissions(operation, args);
        if (operation === 'capabilities') return capabilities(operation, args);
        // No unconfirmed forum REST write is advertised by
        // production composition. There is no bot-mode silent fallback.
        throw new PrivateRpcError('unsupported');
      };
      return client;
    },
    execute: (operation, args) => {
      if (!handler) throw new PrivateRpcError('unavailable', 503);
      return handler(operation, args);
    },
  });
  // A signal handler does not itself close SDK sockets or the database: the
  // one service owner handles ordered Gateway/RPC/SDK task drain.
  let shutdown: (() => void) | undefined;
  const terminated = new Promise<void>(resolve => {shutdown = resolve;});
  const stop = (): void => shutdown?.();
  process.once('SIGTERM', stop);
  process.once('SIGINT', stop);
  try {
    await service.start();
    await terminated;
  } finally {
    process.off('SIGTERM', stop);
    process.off('SIGINT', stop);
    await service.close();
  }
}

/** Invoke only when executed as the Node sidecar, not imported by test tools. */
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  void runFluxerNodeService().catch(() => {
    // SDK/network/provider errors can contain credentials and private URLs.
    process.stderr.write('Fluxer Node sidecar startup or shutdown refused\n');
    process.exitCode = 1;
  });
}
