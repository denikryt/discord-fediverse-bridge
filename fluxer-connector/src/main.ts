/** Standalone one-instance Fluxer SDK sidecar entry point.
 *
 * The operator supplies a vetted instance and a Node-only bot secret file.
 * The Python process receives only the independently configured IPC bearer;
 * physical write handlers use the same SDK session; live server acceptance
 * remains a deployment gate, not an alternative unauthenticated transport.
 */
import {pathToFileURL} from 'node:url';
import {discoverInstance} from './discovery.ts';
import {readBotTokenFromFile} from './token_secret.ts';
import {createTrustedSdkClient} from './sdk_factory.ts';
import {FluxerNodeService} from './service.ts';
import {createSdkReadHandler} from './rest_reads.ts';
import {createSdkPermissionHandler} from './permissions.ts';
import {createSdkCapabilityHandler} from './capabilities.ts';
import {createSdkForumCreateHandler} from './forum_create.ts';
import {createSdkMessageSendHandler} from './message_send.ts';
import {createSdkMessageMutationHandler} from './message_mutations.ts';
import type {Client} from '@fluxerjs/core';
import {PrivateRpcError, validPrivateBearer, type RpcHandler} from './transport.ts';

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
      connectorKey.length > 255 || connectorKey === 'discord-primary' ||
      // Refuse invalid headers before discovery; transport checks again when
      // an RPC owner is directly constructed without the production main.
      !validPrivateBearer(bearer) ||
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

/** Route only supported SDK operations through one authenticated client.
 *
 * The underlying executor performs fresh target/ACL/author verification;
 * there is no direct REST sender in Python and no SDK write fallback.
 */
export function createProductionRpcHandler(
  client: Client,
  routes: {channel(id: string): string; channelThreads(id: string): string},
  options: {writeContractVerified?: boolean} = {},
): RpcHandler {
  const reads = createSdkReadHandler(client);
  const permissions = createSdkPermissionHandler(client);
  const capabilities = createSdkCapabilityHandler(client, routes.channel);
  const forum = createSdkForumCreateHandler(client, routes.channel, routes.channelThreads);
  const send = createSdkMessageSendHandler(client);
  const mutate = createSdkMessageMutationHandler(client);
  return async (operation, args) => {
    if (operation === 'fetch_channel' || operation === 'fetch_thread' ||
        operation === 'fetch_message') return reads(operation, args);
    if (operation === 'check_permissions') return permissions(operation, args);
    if (operation === 'capabilities') {
      const metadata = await capabilities(operation, args);
      // A verified channel kind/tag snapshot is necessary but insufficient:
      // the current bot's ACL may already have been revoked. Contract evidence
      // is an independent operator-controlled live acceptance gate.
      const disabled = {forum_posts: false, thread_replies: false,
        message_edit_delete: false};
      if (!options.writeContractVerified || client.user?.bot !== true ||
          !client.user.id || typeof metadata.channel_id !== 'string') {
        return {...metadata, ...disabled};
      }
      const requested: string[] = [];
      if (metadata.forum_posts === true) requested.push('create_forum_post');
      // Thread sends fetch and verify a parent message before the SDK write.
      // Match the executor's ReadMessageHistory gate in advertised rights.
      if (metadata.thread_replies === true) requested.push('send_message', 'read_history');
      if (metadata.message_edit_delete === true) {
        requested.push('edit_message', 'delete_message');
      }
      if (!requested.length) return {...metadata, ...disabled};
      const access = await permissions('check_permissions', {
        bot_id: client.user.id, channel_id: metadata.channel_id, actions: requested,
      });
      if (access.channel_id !== metadata.channel_id ||
          access.guild_id !== metadata.guild_id ||
          access.bot_id !== client.user.id) return {...metadata, ...disabled};
      const eligibleTags = metadata.requires_tag !== true ||
        (Array.isArray(metadata.available_tags) &&
          metadata.available_tags.some((tag: {moderated: boolean}) => tag.moderated === false));
      return {...metadata,
        forum_posts: metadata.forum_posts === true && eligibleTags &&
          access.create_forum_post === 'allowed',
        thread_replies: metadata.thread_replies === true &&
          access.send_message === 'allowed' && access.read_history === 'allowed',
        message_edit_delete: metadata.message_edit_delete === true &&
          access.edit_message === 'allowed' && access.delete_message === 'allowed',
      };
    }
    // Direct private RPC callers cannot bypass the same server-build evidence
    // gate that is used for capability advertisement. The SDK executors still
    // enforce fresh ACL and ownership after this admission check.
    if (['create_forum_thread', 'send_message', 'edit_message', 'delete_message']
        .includes(operation) && !options.writeContractVerified) {
      throw new PrivateRpcError('unsupported');
    }
    if (operation === 'create_forum_thread') return forum(operation, args);
    if (operation === 'send_message') return send(operation, args);
    if (operation === 'edit_message' || operation === 'delete_message') {
      return mutate(operation, args);
    }
    // The text-message-root thread operation is explicitly outside #12.
    throw new PrivateRpcError('unsupported');
  };
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
      // Remain fail-closed on a new deployment until an isolated real-server
      // acceptance has been recorded for its exact SDK/server build.
      handler = createProductionRpcHandler(client, Routes, {
        writeContractVerified: process.env.FLUXER_VERIFIED_WRITE_CONTRACT === 'true',
      });
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
