/** One-shot bot message/reply delivery through the official SDK channel manager.
 *
 * SDK v3.3.0 `client.channels.send(container, MessageSendOptions)` prepares
 * `replyTo` into the real Fluxer `message_reference` wire format, including
 * same-container identity. This module never translates it to Discord IDs,
 * falls back to a flat message or creates a second REST sender. The Python
 * adapter/coordinator still owns the immutable durable CreateAttempt fence.
 *
 * The supplied server snapshot defines `SEND_MESSAGES_IN_THREADS` at 1<<38;
 * it is absent from public SDK PermissionFlags. The custom bit is only used
 * for fail-closed preflight here, not advertised as general SDK capability.
 * Production must remain disabled until the operator's actual build proves
 * that bit, reply semantics and the bot message response contract.
 */
import {PrivateRpcError, type RpcHandler} from './transport.ts';
import {createSdkPermissionHandler} from './permissions.ts';
import type {Channel, Client, GuildChannel, Message, MessageSendOptions} from '@fluxerjs/core';

const ID = /^[A-Za-z0-9_-]+$/;
const THREAD_SEND_BIT = 1n << 38n;
const KEYS = new Set(['container_id', 'guild_id', 'reply_to_message_id',
  'content', 'allowed_mentions', 'attempt_key', 'delivery_mode']);

/** Use the official manager declarations, including their broad Channel result. */
export type SdkMessageSendClient = Pick<Client, 'user' | 'channels' | 'guilds'>;

/** Reject route fragments and missing physical IDs before SDK method entry. */
function physicalId(value: unknown): string {
  if (typeof value !== 'string' || !ID.test(value)) {
    throw new PrivateRpcError('invalid_request');
  }
  return value;
}

/** Validate a single bot-only plain-text request without dropping extra data. */
function parseRequest(args: Readonly<Record<string, unknown>>): {
  containerId: string; guildId: string; replyId: string | null; body: string;
} {
  if (Object.keys(args).some(key => !KEYS.has(key)) ||
      args.delivery_mode !== 'bot' || typeof args.attempt_key !== 'string' ||
      !args.attempt_key || args.attempt_key.length > 255 ||
      !args.allowed_mentions || typeof args.allowed_mentions !== 'object' ||
      Array.isArray(args.allowed_mentions) ||
      Object.keys(args.allowed_mentions).length !== 1 ||
      !('parse' in args.allowed_mentions) ||
      !Array.isArray(args.allowed_mentions.parse) ||
      args.allowed_mentions.parse.length !== 0) {
    throw new PrivateRpcError('invalid_request');
  }
  const containerId = physicalId(args.container_id);
  const guildId = physicalId(args.guild_id);
  if (args.reply_to_message_id !== null &&
      (typeof args.reply_to_message_id !== 'string' || !ID.test(args.reply_to_message_id))) {
    throw new PrivateRpcError('invalid_request');
  }
  const content = args.content;
  if (!content || typeof content !== 'object' || Array.isArray(content) ||
      Object.keys(content).sort().join(',') !== 'body,format' ||
      !('body' in content) || typeof content.body !== 'string' ||
      !content.body.trim() || !('format' in content) || content.format !== 'plain' ||
      Buffer.byteLength(content.body, 'utf8') > 8192) {
    throw new PrivateRpcError('unsupported');
  }
  return {containerId, guildId,
    replyId: args.reply_to_message_id as string | null,
    body: content.body};
}

/** Make an SDK read result an exact guild-scoped physical container. */
function observedChannel(channel: Channel, id: string, guildId: string): asserts channel is GuildChannel {
  if (!channel || channel.id !== id || !channel.isGuild() ||
      channel.guildId !== guildId) {
    throw new PrivateRpcError('invalid_request');
  }
}

/** Refuse mismatched receipt scope or dropped nested-parent information. */
function verifyMessage(message: Message, containerId: string, guildId: string,
                       replyId: string | null, botId: string,
                       body: string): Record<string, unknown> {
  // A post has already been attempted. Invalid or incomplete server result
  // is uncertain delivery, not a definite rejection permitting a retry.
  if (!message || typeof message.id !== 'string' || !ID.test(message.id) ||
      message.channelId !== containerId || message.guildId !== guildId ||
      message.content !== body || message.author?.id !== botId ||
      message.author.bot !== true || message.webhookId !== null) {
    throw new PrivateRpcError('unknown_delivery');
  }
  if (replyId !== null) {
    if (!message.messageReference ||
        message.messageReference.channelId !== containerId ||
        message.messageReference.messageId !== replyId ||
        (message.messageReference.guildId !== null &&
          message.messageReference.guildId !== undefined &&
          message.messageReference.guildId !== guildId)) {
      throw new PrivateRpcError('unknown_delivery');
    }
  } else if (message.messageReference !== null) {
    throw new PrivateRpcError('unknown_delivery');
  }
  return {container_id: containerId, message_id: message.id, delivery_mode: 'bot'};
}

/** Send one exact physical message using the pinned SDK's public send API. */
export function createSdkMessageSendHandler(client: SdkMessageSendClient): RpcHandler {
  if (typeof client?.channels?.send !== 'function' ||
      typeof client?.channels?.fetchMessage !== 'function') {
    throw new Error('Fluxer SDK message methods unavailable');
  }
  const acl = createSdkPermissionHandler(client);
  return async (operation, args) => {
    if (operation !== 'send_message') throw new PrivateRpcError('unsupported');
    const {containerId, guildId, replyId, body} = parseRequest(args);
    const botId = client.user?.id;
    if (client.user?.bot !== true || typeof botId !== 'string' || !ID.test(botId)) {
      throw new PrivateRpcError('forbidden');
    }
    const channel = await client.channels.fetch(containerId, {force: true});
    observedChannel(channel, containerId, guildId);
    const thread = channel.isThread();
    if (!thread && !channel.isText() && !channel.isAnnouncement()) {
      throw new PrivateRpcError('unsupported');
    }
    const actions = thread ? ['view_channel', 'read_history'] :
      ['view_channel', 'send_message'];
    const permission = await acl('check_permissions', {
      channel_id: containerId, bot_id: botId, actions,
    });
    if (permission.guild_id !== guildId ||
        actions.some(action => permission[action] !== 'allowed')) {
      throw new PrivateRpcError('forbidden');
    }
    if (channel.isThread()) {
      // Forum posts are in a public thread, not their parent forum. A thread
      // cannot inherit proof of SEND_MESSAGES_IN_THREADS from SendMessages.
      if (channel.archived !== false || channel.locked !== false ||
          !channel.parentId || !ID.test(channel.parentId)) {
        throw new PrivateRpcError('unsupported');
      }
      const parent = await client.channels.fetch(channel.parentId, {force: true});
      observedChannel(parent, channel.parentId, guildId);
      if (!parent.isForum() && !parent.isText() && !parent.isAnnouncement()) {
        throw new PrivateRpcError('unsupported');
      }
      const parentAccess = await acl('check_permissions', {
        channel_id: parent.id, bot_id: botId, actions: ['view_channel'],
      });
      if (parentAccess.guild_id !== guildId || parentAccess.view_channel !== 'allowed') {
        throw new PrivateRpcError('forbidden');
      }
      // Public SDK PermissionFlags do not name bit 38, but SDK
      // PermissionsBitField.has(bigint) accepts the server-defined bit. This
      // narrow check is *not* used to advertise global capabilities.
      const guild = await client.guilds.fetch(guildId, {force: true});
      if (!guild || guild.id !== guildId) {
        throw new PrivateRpcError('forbidden');
      }
      const me = await guild.members?.fetch(botId, {force: true});
      if (!me || me.id !== botId || me.guild?.id !== guildId ||
          me.permissionsIn(channel)?.has(THREAD_SEND_BIT) !== true) {
        throw new PrivateRpcError('forbidden');
      }
    }
    if (replyId !== null) {
      // An immediate-container match is necessary even if the earlier
      // Python caller already resolved a canonical/physical parent mapping.
      const replied = await client.channels.fetchMessage(containerId, replyId);
      if (!replied || replied.id !== replyId ||
          replied.channelId !== containerId || replied.guildId !== guildId) {
        throw new PrivateRpcError('not_found', 404);
      }
    }
    const payload: MessageSendOptions = {content: body,
      allowedMentions: {parse: [], repliedUser: false}};
    if (replyId !== null) {
      payload.replyTo = {channelId: containerId, messageId: replyId, guildId};
    }
    let sent: Message;
    try {
      // Exactly one SDK send. Its own MessageUtils maps replyTo to the
      // wire `message_reference` and suppresses all reply notifications.
      sent = await client.channels.send(containerId, payload);
    } catch {
      throw new PrivateRpcError('unknown_delivery');
    }
    return verifyMessage(sent, containerId, guildId, replyId, botId, body);
  };
}
