/** Scoped SDK v3.3.0 channel, thread and message reads for the private RPC port.
 *
 * This is a transport-only read boundary. It does not infer ownership from
 * cache, create ActivityPub objects or perform a physical mutation. The real
 * SDK's ChannelManager.fetch(id, {force:true}) and fetchMessage(container,id)
 * are the only external operations used here.
 */
import {PrivateRpcError, type RpcHandler} from './transport.ts';

/** Only the public read methods/properties of the pinned SDK used by this module. */
type SdkChannel = {
  id: string;
  name: string | null;
  isForum(): boolean;
  isGuild(): boolean;
  isThread(): boolean;
  isText(): boolean;
  isAnnouncement(): boolean;
};
type SdkMessage = {
  id: string;
  channelId: string;
  guildId: string | null;
  content: string;
  author: {id: string; username: string; bot: boolean};
  webhookId?: string | null;
  createdAt: Date;
  editedAt?: Date | null;
  attachments: Map<string, unknown>;
};
export type SdkReadClient = {
  channels: {
    fetch(id: string, options: {force: true}): Promise<SdkChannel>;
    fetchMessage(containerId: string, id: string): Promise<SdkMessage>;
  };
};

/** Validate the physical ID subset accepted in the current Node RPC boundary. */
function physicalId(value: unknown): string {
  // Reject route separators and control characters before SDK route construction.
  if (typeof value !== 'string' || !value || !/^[A-Za-z0-9_-]+$/.test(value)) {
    throw new PrivateRpcError('invalid_request');
  }
  return value;
}

/** Refuse implicit scope or mode switching through unexpected request fields. */
function readArgs(args: Readonly<Record<string, unknown>>, keys: readonly string[]): void {
  // Unknown fields cannot silently change an SDK operation's physical scope.
  if (Object.keys(args).some(key => !keys.includes(key))) {
    throw new PrivateRpcError('invalid_request');
  }
}

/** Validate an observed channel against an exact requested ID and guild scope. */
function requireChannelIdentity(channel: SdkChannel, expectedId: string): string {
  // SDK cache may have stale or inconsistent objects: validate every result.
  if (physicalId(channel.id) !== expectedId) {
    throw new PrivateRpcError('invalid_request');
  }
  // The SDK Channel base type has no guildId; only narrowed guild channels
  // expose one. Do not assume that a DM or unknown channel is in a guild.
  if (!channel.isGuild()) return '';
  const guildId = 'guildId' in channel ? channel.guildId : null;
  return guildId == null ? '' : physicalId(guildId);
}

/** Classify only forum/text/announcement kinds supported by the neutral port. */
function channelKind(channel: SdkChannel): string {
  if (channel.isForum()) return 'forum';
  if (channel.isText()) return 'text';
  if (channel.isAnnouncement()) return 'announcement';
  return 'unknown';
}

/** Translate three verified SDK read operations to exact Python RPC snapshots. */
export function createSdkReadHandler(client: SdkReadClient): RpcHandler {
  if (!client?.channels || typeof client.channels.fetch !== 'function' ||
      typeof client.channels.fetchMessage !== 'function') {
    throw new Error('Fluxer SDK read methods unavailable');
  }

  return async (operation, args) => {
    if (operation === 'fetch_channel') {
      readArgs(args, ['id']);
      const id = physicalId(args.id);
      const channel = await client.channels.fetch(id, {force: true});
      const guildId = requireChannelIdentity(channel, id);
      if (channel.name !== null && typeof channel.name !== 'string') {
        throw new PrivateRpcError('invalid_request');
      }
      return {id, kind: channelKind(channel), guild_id: guildId || null,
        name: channel.name};
    }

    if (operation === 'fetch_thread') {
      readArgs(args, ['id']);
      const id = physicalId(args.id);
      const thread = await client.channels.fetch(id, {force: true});
      const guildId = requireChannelIdentity(thread, id);
      const observedParent = 'parentId' in thread ? thread.parentId : null;
      if (!thread.isThread() || !guildId || !observedParent) {
        throw new PrivateRpcError('invalid_request');
      }
      const parentId = physicalId(observedParent);
      const parent = await client.channels.fetch(parentId, {force: true});
      const parentGuild = requireChannelIdentity(parent, parentId);
      if (parentGuild !== guildId) throw new PrivateRpcError('invalid_request');
      const kind = parent.isForum() ? 'forum' :
        (parent.isText() || parent.isAnnouncement()) ? 'message' : null;
      if (!kind) throw new PrivateRpcError('unsupported');
      if (thread.name !== null && typeof thread.name !== 'string') {
        throw new PrivateRpcError('invalid_request');
      }
      // SDK ThreadChannel has no confirmed starter-message ID property.
      // Unknown starter identity must never be substituted with thread ID.
      return {id, parent_id: parentId, guild_id: guildId,
        name: thread.name, kind, starter_id: null};
    }

    if (operation === 'fetch_message') {
      readArgs(args, ['id', 'container_id']);
      const id = physicalId(args.id);
      const containerId = physicalId(args.container_id);
      const container = await client.channels.fetch(containerId, {force: true});
      const guildId = requireChannelIdentity(container, containerId);
      if (!guildId || !(container.isThread() || container.isText() ||
          container.isAnnouncement())) {
        throw new PrivateRpcError('invalid_request');
      }
      const message = await client.channels.fetchMessage(containerId, id);
      if (physicalId(message.id) !== id ||
          physicalId(message.channelId) !== containerId ||
          message.guildId !== guildId) {
        throw new PrivateRpcError('invalid_request');
      }
      if (!(message.attachments instanceof Map)) {
        throw new PrivateRpcError('invalid_request');
      }
      // The Python adapter intentionally does not accept attachment-bearing
      // snapshots until the media contract is verified on a Fluxer build.
      if (message.attachments.size) throw new PrivateRpcError('unsupported');
      if (typeof message.content !== 'string' || !message.author ||
          typeof message.author.bot !== 'boolean' ||
          typeof message.author.username !== 'string') {
        throw new PrivateRpcError('invalid_request');
      }
      const authorId = physicalId(message.author.id);
      const created = message.createdAt;
      const edited = message.editedAt ?? null;
      if (!(created instanceof Date) || !Number.isFinite(created.getTime()) ||
          (edited !== null && (!(edited instanceof Date) ||
            !Number.isFinite(edited.getTime())))) {
        throw new PrivateRpcError('invalid_request');
      }
      const webhook = message.webhookId !== null && message.webhookId !== undefined;
      if (webhook) physicalId(message.webhookId);
      return {id, container_id: containerId, content: message.content,
        author: {id: authorId, kind: webhook ? 'webhook' :
          message.author.bot ? 'bot' : 'human',
          display_name: message.author.username},
        created_at: created.toISOString(), edited_at: edited?.toISOString() ?? null,
        attachments: []};
    }

    // Writes, permissions and capabilities belong to separate checkpoint
    // executors. Never silently treat an unsupported mutation as a read.
    throw new PrivateRpcError('unsupported');
  };
}
