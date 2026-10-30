/** Mutate only the authenticated bot's exact existing Fluxer message.
 *
 * Fluxer SDK 3.3.0 Message.edit()/delete() own the physical REST request,
 * rate-limit behavior and response mapping. This executor performs fresh
 * authorization/ownership reads before SDK method entry; the Python mutation
 * coordinator remains the only durable claim and revision-fencing owner.
 * An uncertain SDK write or untrusted success response never becomes an ACK.
 * Live server verification is still required before production activation.
 */
import {PrivateRpcError, type RpcHandler} from './transport.ts';
import {createSdkPermissionHandler} from './permissions.ts';
import type {Client, Message} from '@fluxerjs/core';

const ID = /^[A-Za-z0-9_-]+$/;
const REF_KEYS = new Set(['container_id', 'message_id', 'bot_id']);
const EDIT_KEYS = new Set([...REF_KEYS, 'content', 'allowed_mentions']);

/** Keep the SDK's broad Channel fetch type and narrow it at the read boundary. */
export type SdkMessageMutationClient = Pick<Client, 'user' | 'channels' | 'guilds'>;

/** Never allow a route fragment or a missing opaque physical ID. */
function physicalId(value: unknown): string {
  if (typeof value !== 'string' || !ID.test(value)) {
    throw new PrivateRpcError('invalid_request');
  }
  return value;
}

/** Reject unsupported content/mentions instead of silently dropping fields. */
function parseArgs(operation: string, args: Readonly<Record<string, unknown>>): {
  containerId: string; messageId: string; botId: string; content?: string;
} {
  if (operation !== 'edit_message' && operation !== 'delete_message') {
    throw new PrivateRpcError('unsupported');
  }
  const keys = operation === 'edit_message' ? EDIT_KEYS : REF_KEYS;
  if (Object.keys(args).some(key => !keys.has(key))) {
    throw new PrivateRpcError('invalid_request');
  }
  const containerId = physicalId(args.container_id);
  const messageId = physicalId(args.message_id);
  const botId = physicalId(args.bot_id);
  if (operation === 'delete_message') return {containerId, messageId, botId};

  const content = args.content;
  if (!content || typeof content !== 'object' || Array.isArray(content) ||
      Object.keys(content).sort().join(',') !== 'body,format' ||
      !('body' in content) || typeof content.body !== 'string' ||
      !content.body.trim() || !('format' in content) || content.format !== 'plain' ||
      Buffer.byteLength(content.body, 'utf8') > 8192) {
    throw new PrivateRpcError('unsupported');
  }
  const mentions = args.allowed_mentions;
  if (!mentions || typeof mentions !== 'object' || Array.isArray(mentions) ||
      Object.keys(mentions).length !== 1 || !('parse' in mentions) ||
      !Array.isArray(mentions.parse) || mentions.parse.length !== 0) {
    throw new PrivateRpcError('invalid_request');
  }
  return {containerId, messageId, botId, content: content.body};
}

/** Classify SDK HTTP errors by their published statusCode, not message text. */
function refusal(error: unknown, writing: boolean): PrivateRpcError {
  if (error instanceof PrivateRpcError) return error;
  // The SDK's MessageManager maps GET 404 to a FluxerError with a
  // stable code, while other HTTP failures may be preserved as `cause`.
  // Examine only this short published error chain; never parse messages.
  let observed = error;
  for (let depth = 0; depth < 3; depth++) {
    if (!observed || typeof observed !== 'object') break;
    if ('code' in observed && typeof observed.code === 'string' &&
        ['MESSAGE_NOT_FOUND', 'CHANNEL_NOT_FOUND', 'GUILD_NOT_FOUND'].includes(observed.code)) {
      return new PrivateRpcError('not_found', 404);
    }
    const code = 'statusCode' in observed && typeof observed.statusCode === 'number'
      ? observed.statusCode : null;
    if (code === 400 || code === 422) return new PrivateRpcError('invalid_request', code);
    if (code === 401 || code === 403) return new PrivateRpcError('forbidden', code);
    if (code === 404) return new PrivateRpcError('not_found', 404);
    if (code === 429) return new PrivateRpcError('rate_limited', 429);
    observed = 'cause' in observed ? observed.cause : null;
  }
  // Before method entry, a failed read cannot mutate remote state. Once
  // editing/deleting begins, even a broken connection may follow a write.
  return new PrivateRpcError(writing ? 'unknown_delivery' : 'unavailable', 503);
}

/** Check exact container, bot provenance and current actor before a write. */
function authorizedMessage(message: Message, containerId: string,
                           messageId: string, guildId: string, botId: string): boolean {
  return Boolean(message && message.id === messageId &&
    message.channelId === containerId && message.guildId === guildId &&
    message.author?.id === botId && message.author.bot === true &&
    message.webhookId === null);
}

/** Return one bot-only mutation handler over SDK Message.edit/delete(). */
export function createSdkMessageMutationHandler(client: SdkMessageMutationClient): RpcHandler {
  if (typeof client?.channels?.fetch !== 'function' ||
      typeof client.channels.fetchMessage !== 'function') {
    throw new Error('Fluxer SDK physical message methods unavailable');
  }
  const acl = createSdkPermissionHandler(client);
  return async (operation, args) => {
    const {containerId, messageId, botId, content} = parseArgs(operation, args);
    if (client.user?.bot !== true || client.user.id !== botId) {
      throw new PrivateRpcError('forbidden');
    }
    let guildId: string;
    let message: Message;
    try {
      const channel = await client.channels.fetch(containerId, {force: true});
      if (!channel || channel.id !== containerId || !channel.isGuild() ||
          !channel.guildId || !ID.test(channel.guildId) ||
          !(channel.isThread() || channel.isText() || channel.isAnnouncement())) {
        throw new PrivateRpcError('invalid_request');
      }
      guildId = channel.guildId;
      const permission = await acl('check_permissions', {
        bot_id: botId, channel_id: containerId,
        actions: ['view_channel', 'read_history'],
      });
      if (permission.guild_id !== guildId ||
          permission.view_channel !== 'allowed' ||
          permission.read_history !== 'allowed') {
        throw new PrivateRpcError('forbidden');
      }
      // fetchMessage is an unconditional SDK REST GET, not a cache hit.
      message = await client.channels.fetchMessage(containerId, messageId);
    } catch (error) {
      throw refusal(error, false);
    }
    if (!authorizedMessage(message, containerId, messageId, guildId, botId) ||
        client.user?.id !== botId || client.user.bot !== true) {
      throw new PrivateRpcError('forbidden');
    }
    // SDK Message instances, not arbitrary request DTOs, perform the write.
    // The caller already holds the durable per-copy claim and uncertainty fence.
    try {
      if (operation === 'edit_message') {
        if (typeof message.edit !== 'function') throw new PrivateRpcError('unsupported');
        const updated = await message.edit({content: content!, allowedMentions: {parse: []}});
        // An HTTP success with mismatched scope or altered content must not
        // advance a durable canonical content revision.
        if (!authorizedMessage(updated, containerId, messageId, guildId, botId) ||
            updated.content !== content) {
          throw new PrivateRpcError('unknown_delivery');
        }
      } else {
        if (typeof message.delete !== 'function') throw new PrivateRpcError('unsupported');
        await message.delete();
      }
    } catch (error) {
      throw refusal(error, true);
    }
    return {container_id: containerId, message_id: messageId};
  };
}
