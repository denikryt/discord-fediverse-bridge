/** One-shot bot forum-post creation through the official Fluxer SDK REST client.
 *
 * SDK v3.3.0's ForumChannel.threads.createPost() drops `response.message`
 * when returning ThreadChannel. Its own ThreadManager.ts issues
 * `rest.post(Routes.channelThreads(id), {body:{name,type:11,message,...}})`;
 * the supplied Fluxer ThreadCreationService returns that original starter in
 * the same successful response. Preserve both physical IDs without guessing.
 *
 * No second REST client, physical retry, metadata-based adoption or durable
 * claim system is introduced here. The Python coordinator remains the only
 * physical CreateAttempt owner. Production capability remains false until the
 * operator's exact live server build is measured, as required by the plan.
 */
import {PrivateRpcError, type RpcHandler} from './transport.ts';
import {createSdkCapabilityHandler, type SdkCapabilityClient} from './capabilities.ts';
import {createSdkPermissionHandler, type SdkPermissionClient} from './permissions.ts';

const ID = /^[A-Za-z0-9_-]+$/;
const REQUEST_KEYS = new Set([
  'forum_id', 'guild_id', 'title', 'content', 'allowed_mentions',
  'attempt_key', 'delivery_mode', 'applied_tags',
]);
const PUBLIC_THREAD = 11; // SDK v3.3.0 ChannelType.PublicThread

/** Minimum published Fluxer SDK read, permission and REST surface for one post. */
export type SdkForumCreateClient = SdkCapabilityClient & SdkPermissionClient & {
  user: {id: string; bot: boolean} | null;
  rest: SdkCapabilityClient['rest'] & {
    post(route: string, options: {auth: true; body: Record<string, unknown>}): Promise<unknown>;
  };
};

/** Admit only opaque nonempty physical IDs, not route fragments or control data. */
function physicalId(value: unknown): string {
  if (typeof value !== 'string' || !ID.test(value)) {
    throw new PrivateRpcError('invalid_request');
  }
  return value;
}

/** Require the immutable one-shot RPC shape without silently dropping data. */
function validateRequest(args: Readonly<Record<string, unknown>>): {
  forumId: string; guildId: string; title: string; body: string; tags: string[];
} {
  if (Object.keys(args).some(key => !REQUEST_KEYS.has(key)) ||
      args.delivery_mode !== 'bot' ||
      typeof args.attempt_key !== 'string' || !args.attempt_key ||
      args.attempt_key.length > 255 ||
      !args.allowed_mentions || typeof args.allowed_mentions !== 'object' ||
      Array.isArray(args.allowed_mentions) ||
      Object.keys(args.allowed_mentions).length !== 1 ||
      !('parse' in args.allowed_mentions) ||
      !Array.isArray(args.allowed_mentions.parse) ||
      args.allowed_mentions.parse.length !== 0) {
    throw new PrivateRpcError('invalid_request');
  }
  const forumId = physicalId(args.forum_id);
  const guildId = physicalId(args.guild_id);
  if (typeof args.title !== 'string' || !args.title.trim() ||
      args.title.length > 100 || args.title !== args.title.trim()) {
    throw new PrivateRpcError('invalid_request');
  }
  const content = args.content;
  if (!content || typeof content !== 'object' || Array.isArray(content) ||
      Object.keys(content).sort().join(',') !== 'body,format' ||
      !('body' in content) || typeof content.body !== 'string' ||
      !content.body.trim() || !('format' in content) || content.format !== 'plain' ||
      Buffer.byteLength(content.body, 'utf8') > 8192) {
    throw new PrivateRpcError('invalid_request');
  }
  const tags = args.applied_tags;
  if (!Array.isArray(tags) || tags.length > 5 ||
      tags.some(tag => typeof tag !== 'string' || !ID.test(tag)) ||
      new Set(tags).size !== tags.length) {
    throw new PrivateRpcError('invalid_request');
  }
  return {forumId, guildId, title: args.title, body: content.body, tags};
}

/** Check an observed server response without inventing its opening message. */
function verifyReceipt(raw: unknown, forumId: string, guildId: string,
                       botId: string): Record<string, unknown> {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new PrivateRpcError('unknown_delivery');
  }
  const thread = raw as Record<string, unknown>;
  const message = thread.message;
  if (!message || typeof message !== 'object' || Array.isArray(message)) {
    throw new PrivateRpcError('unknown_delivery');
  }
  const starter = message as Record<string, unknown>;
  const author = starter.author;
  if (!author || typeof author !== 'object' || Array.isArray(author)) {
    throw new PrivateRpcError('unknown_delivery');
  }
  const source = author as Record<string, unknown>;
  // Validate after the POST as an unknown effect, not a definite refusal:
  // the server may have already created the thread and starter.
  if (typeof thread.id !== 'string' || !ID.test(thread.id) ||
      thread.parent_id !== forumId || thread.guild_id !== guildId ||
      thread.type !== PUBLIC_THREAD ||
      typeof starter.id !== 'string' || !ID.test(starter.id) ||
      starter.channel_id !== thread.id || starter.guild_id !== guildId ||
      source.id !== botId || source.bot !== true ||
      starter.webhook_id !== null) {
    throw new PrivateRpcError('unknown_delivery');
  }
  return {forum_id: forumId, guild_id: guildId, thread_id: thread.id,
    starter_id: starter.id, delivery_mode: 'bot'};
}

/** Wire a single SDK REST forum POST to the authenticated private RPC handler. */
export function createSdkForumCreateHandler(
  client: SdkForumCreateClient,
  channelRoute: (id: string) => string,
  threadsRoute: (id: string) => string,
): RpcHandler {
  const readCapabilities = createSdkCapabilityHandler(client, channelRoute);
  const readPermissions = createSdkPermissionHandler(client);
  if (typeof client.rest.post !== 'function' || typeof threadsRoute !== 'function') {
    throw new Error('Fluxer SDK forum REST methods unavailable');
  }
  return async (operation, args) => {
    if (operation !== 'create_forum_thread') throw new PrivateRpcError('unsupported');
    const {forumId, guildId, title, body, tags} = validateRequest(args);
    const botId = client.user?.id;
    if (client.user?.bot !== true || typeof botId !== 'string' || !ID.test(botId)) {
      throw new PrivateRpcError('forbidden');
    }
    // Both channel metadata and role overwrites must be freshly observed.
    // SDK channel cache fields alone cannot prove required-tag policy.
    const metadata = await readCapabilities('capabilities', {channel_id: forumId});
    if (metadata.guild_id !== guildId || metadata.tag_requirements_verified !== true ||
        !Array.isArray(metadata.available_tags) ||
        typeof metadata.requires_tag !== 'boolean') {
      throw new PrivateRpcError('unsupported');
    }
    if (metadata.requires_tag && tags.length === 0) {
      throw new PrivateRpcError('unsupported');
    }
    const available = metadata.available_tags as {id: string; moderated: boolean}[];
    if (tags.some(tag => !available.some(item => item.id === tag && !item.moderated))) {
      throw new PrivateRpcError('unsupported');
    }
    const permissions = await readPermissions('check_permissions', {
      bot_id: botId, channel_id: forumId, actions: ['view_channel', 'send_message'],
    });
    if (permissions.guild_id !== guildId ||
        permissions.view_channel !== 'allowed' || permissions.send_message !== 'allowed') {
      throw new PrivateRpcError('forbidden');
    }
    // No SDK or RPC retries of an ambiguous POST. The wrapper deliberately
    // bypasses createPost() only because that public helper drops the starter.
    // The operation, route and wire body are sourced from SDK v3.3.0 itself.
    let result: unknown;
    try {
      result = await client.rest.post(threadsRoute(forumId), {
        auth: true, body: {name: title, type: PUBLIC_THREAD,
          message: {content: body, allowed_mentions: {parse: []}}, applied_tags: tags},
      });
    } catch {
      // A failed response cannot prove the physical write was not accepted.
      throw new PrivateRpcError('unknown_delivery');
    }
    return verifyReceipt(result, forumId, guildId, botId);
  };
}
