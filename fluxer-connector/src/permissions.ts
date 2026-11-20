/** Conservative bot ACL checks using Fluxer SDK v3.3.0 member permissions.
 *
 * Channel overwrites are applied by GuildMember.permissionsIn(), not by a
 * bridge-specific bitfield implementation. Physical writes remain unauthorized
 * when the SDK cannot prove an action-specific right or role completeness.
 */
import {PrivateRpcError, type RpcHandler} from './transport.ts';

// Fluxer permission bit 38 is not named in SDK v3.3.0's public enum.
const SEND_MESSAGES_IN_THREADS = 1n << 38n;

const ACTIONS = new Set([
  'view_channel', 'read_history', 'create_forum_post',
  'start_message_thread', 'send_message', 'edit_message', 'delete_message',
]);

type SdkChannelAcl = {
  id: string;
  isGuild(): boolean;
  isThread(): boolean;
  isForum(): boolean;
};
type SdkMemberAcl = {
  id: string;
  guild: {id: string};
  roles: {roleIds: readonly string[]};
  permissionsIn(channel: SdkChannelAcl): {has(flag: string | bigint): boolean};
};
type SdkGuildAcl = {
  id: string;
  fetchRoles(): Promise<readonly {id: string}[]>;
  members: {fetch(userId: string, options: {force: true}): Promise<SdkMemberAcl>};
};
export type SdkPermissionClient = {
  user: {id: string; bot: boolean} | null;
  channels: {fetch(id: string, options: {force: true}): Promise<SdkChannelAcl>};
  guilds: {fetch(id: string, options: {force: true}): Promise<unknown>};
};

/** Verify identifier shape without ever substituting a different SDK scope. */
function physicalId(input: unknown): string {
  if (typeof input !== 'string' || !/^[A-Za-z0-9_-]+$/.test(input)) {
    throw new PrivateRpcError('invalid_request');
  }
  return input;
}

/** Extract the SDK guild identity only from a true guild-channel instance. */
function channelGuild(channel: SdkChannelAcl, id: string): string {
  if (!channel || physicalId(channel.id) !== id || !channel.isGuild()) {
    throw new PrivateRpcError('invalid_request');
  }
  const guild = 'guildId' in channel ? channel.guildId : null;
  return physicalId(guild);
}

/** Resolve only action-specific bot ACL, not server-supported write capability.
 *
 * Message ownership, forum tag policy, and actual REST write support are
 * independent checks at the physical executor and capability boundaries.
 */
function decide(action: string, can: (flag: string) => boolean,
                thread: boolean, forum: boolean, canThreadSend: boolean): string {
  if (action === 'view_channel') return can('ViewChannel') ? 'allowed' : 'denied';
  if (action === 'read_history') {
    return can('ViewChannel') && can('ReadMessageHistory') ? 'allowed' : 'denied';
  }
  if (action === 'create_forum_post') {
    return forum && can('ViewChannel') && can('SendMessages') ? 'allowed' : 'denied';
  }
  if (action === 'send_message') {
    if (!can('ViewChannel')) return 'denied';
    // A thread send needs bit 38 on the thread itself. A parent forum's
    // ordinary SendMessages bit neither grants nor revokes that thread bit.
    return thread ? (canThreadSend ? 'allowed' : 'denied') :
      (can('SendMessages') ? 'allowed' : 'denied');
  }
  if (action === 'start_message_thread') {
    if (!can('ViewChannel') || !can('ReadMessageHistory')) return 'denied';
    // Starting a text-message thread is deliberately unsupported in #12.
    return 'unknown';
  }
  if (action === 'edit_message' || action === 'delete_message') {
    // Viewing history is necessary to fetch and verify the exact bot-owned
    // target. This does not authorize edits to any other author's messages.
    return can('ViewChannel') && can('ReadMessageHistory') ? 'allowed' : 'denied';
  }
  return 'unknown';
}

/** Build one actual SDK `check_permissions` operation for the private RPC. */
export function createSdkPermissionHandler(client: SdkPermissionClient): RpcHandler {
  if (!client?.channels || !client.guilds ||
      typeof client.channels.fetch !== 'function' ||
      typeof client.guilds.fetch !== 'function') {
    throw new Error('Fluxer SDK permissions methods unavailable');
  }
  return async (operation, args) => {
    if (operation !== 'check_permissions') throw new PrivateRpcError('unsupported');
    if (Object.keys(args).some(k => !['bot_id','channel_id','actions'].includes(k)) ||
        !Array.isArray(args.actions) ||
        args.actions.some(a => typeof a !== 'string' || !ACTIONS.has(a)) ||
        new Set(args.actions).size !== args.actions.length) {
      throw new PrivateRpcError('invalid_request');
    }
    const botId = physicalId(args.bot_id);
    const channelId = physicalId(args.channel_id);
    const result: Record<string,string|null> = {bot_id:botId, channel_id:channelId, guild_id:null};
    for (const action of args.actions) result[action] = 'unknown';

    // An unverified or foreign principal may inspect no SDK guild state and
    // must never be treated as a valid bot authorization source.
    if (client.user?.bot !== true || client.user.id !== botId) return result;
    const channel = await client.channels.fetch(channelId, {force:true});
    const guildId = channelGuild(channel, channelId);
    result.guild_id = guildId;
    const observedGuild = await client.guilds.fetch(guildId, {force:true});
    if (!observedGuild || typeof observedGuild !== 'object' ||
        !('id' in observedGuild) || observedGuild.id !== guildId ||
        !('members' in observedGuild) || !('fetchRoles' in observedGuild)) {
      throw new PrivateRpcError('invalid_request');
    }
    const guild = observedGuild as SdkGuildAcl;
    if (typeof guild.members?.fetch !== 'function' ||
        typeof guild.fetchRoles !== 'function') {
      throw new PrivateRpcError('invalid_request');
    }

    const member = await guild.members.fetch(botId, {force:true});
    if (!member || member.id !== botId || member.guild?.id !== guildId ||
        !Array.isArray(member.roles?.roleIds)) {
      throw new PrivateRpcError('invalid_request');
    }
    // Fresh role retrieval is necessary: cached roles can outlive a revoke.
    const roles = await guild.fetchRoles();
    if (!Array.isArray(roles)) throw new PrivateRpcError('invalid_request');
    const freshRoleIds = new Set(roles.map(role => physicalId(role.id)));
    if (!freshRoleIds.has(guildId) ||
        member.roles.roleIds.some(id => !freshRoleIds.has(id))) {
      return result;
    }

    const evaluated = [channel];
    if (channel.isThread()) {
      const parentId = 'parentId' in channel ? physicalId(channel.parentId) : null;
      if (!parentId) return result;
      const parent = await client.channels.fetch(parentId, {force:true});
      if (channelGuild(parent, parentId) !== guildId) {
        throw new PrivateRpcError('invalid_request');
      }
      evaluated.push(parent);
    }
    const effective = evaluated.map(scope => member.permissionsIn(scope));
    if (effective.some(rights => !rights || typeof rights.has !== 'function')) {
      throw new PrivateRpcError('invalid_request');
    }
    const can = (flag: string) => effective.every(rights => rights.has(flag));
    const canThreadSend = channel.isThread() && effective[0].has(SEND_MESSAGES_IN_THREADS);
    for (const action of args.actions) {
      result[action] = decide(action, can, channel.isThread(),
        channel.isForum(), canThreadSend);
    }
    return result;
  };
}
