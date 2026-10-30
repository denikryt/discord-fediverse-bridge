/** Loopback provider exercising the actual Python→Node private RPC wire. */
import { PrivateRpcService, PrivateRpcError } from '../src/transport.ts';

let sequence = 0;
const server = new PrivateRpcService({
  connectorKey: 'fluxer-contract', transportSecret: 'test-transport-secret',
  handler: async (method, args) => {
    if (method === 'fetch_channel') {
      if (args.id !== 'forum-1') throw new PrivateRpcError('not_found', 404);
      return {id: 'forum-1', kind: 'forum', guild_id: 'guild-1'};
    }
    if (method === 'create_forum_thread') {
      sequence += 1;
      return {thread_id: `thread-${sequence}`, starter_id: `message-${sequence}`};
    }
    throw new PrivateRpcError('unsupported');
  },
});
const port = await server.start();
process.stdout.write(`${port}\n`);
process.on('SIGTERM', () => { void server.close().then(() => process.exit(0)); });
