/** Read scoped Fluxer SDK forum metadata without assuming build-level write support.
 *
 * SDK v3.3.0 caches forum fields and may default an omitted flags field to
 * zero. A raw channel GET via the SDK REST client therefore confirms that
 * tags and flags really occurred in the current response. The supplied
 * Fluxer server source defines REQUIRE_TAG at bit 4, but its build is not
 * verified as the operator's live server; write capabilities remain false.
 */
import {PrivateRpcError, type RpcHandler} from './transport.ts';

const REQUIRE_TAG = 1 << 4;
const ID = /^[A-Za-z0-9_-]+$/;

type SdkCapabilityChannel = {
  id: string;
  type: number;
  isGuild(): boolean;
  isForum(): boolean;
  isThread(): boolean;
  isText(): boolean;
  isAnnouncement(): boolean;
};
export type SdkCapabilityClient = {
  channels: {fetch(id: string, options: {force: true}): Promise<SdkCapabilityChannel>};
  rest: {get(route: string): Promise<unknown>};
};

/** Accept no route characters or empty identifiers from the private RPC. */
function physicalId(value: unknown): string {
  if (typeof value !== 'string' || !ID.test(value)) {
    throw new PrivateRpcError('invalid_request');
  }
  return value;
}

/** Validate the entire current forum tag snapshot before exposing any tags. */
function forumTags(rawChannel: Record<string, unknown>): {
  verified: boolean; requiresTag: boolean | null;
  availableTags: {id: string; moderated: boolean}[];
} {
  const flags = rawChannel.flags;
  const rawTags = rawChannel.available_tags;
  if (!Number.isSafeInteger(flags) || typeof flags !== 'number' || flags < 0 ||
      !Array.isArray(rawTags) || rawTags.length > 20) {
    return {verified: false, requiresTag: null, availableTags: []};
  }
  const tags: {id: string; moderated: boolean}[] = [];
  const seen = new Set<string>();
  for (const value of rawTags) {
    if (!value || typeof value !== 'object' || !('id' in value) ||
        typeof value.id !== 'string' || !ID.test(value.id) ||
        !('moderated' in value) || typeof value.moderated !== 'boolean' ||
        seen.has(value.id)) {
      // One malformed entry invalidates the whole policy, not just that tag.
      return {verified: false, requiresTag: null, availableTags: []};
    }
    seen.add(value.id);
    tags.push({id: value.id, moderated: value.moderated});
  }
  return {verified: true, requiresTag: (flags & REQUIRE_TAG) !== 0,
    availableTags: tags};
}

/** Expose only fresh SDK metadata and conservatively false write capabilities. */
export function createSdkCapabilityHandler(
  client: SdkCapabilityClient, channelRoute: (id: string) => string,
): RpcHandler {
  if (!client?.channels || typeof client.channels.fetch !== 'function' ||
      typeof client.rest?.get !== 'function' || typeof channelRoute !== 'function') {
    throw new Error('Fluxer SDK capabilities read method unavailable');
  }
  return async (operation, args) => {
    if (operation !== 'capabilities') throw new PrivateRpcError('unsupported');
    if (Object.keys(args).some(key => key !== 'channel_id')) {
      throw new PrivateRpcError('invalid_request');
    }
    const channelId = physicalId(args.channel_id);
    const channel = await client.channels.fetch(channelId, {force: true});
    if (!channel || physicalId(channel.id) !== channelId || !channel.isGuild()) {
      throw new PrivateRpcError('invalid_request');
    }
    const guildId = 'guildId' in channel ? physicalId(channel.guildId) : null;
    if (!guildId) throw new PrivateRpcError('invalid_request');
    let observed: ReturnType<typeof forumTags> =
      {verified: false, requiresTag: null, availableTags: []};
    if (channel.isForum()) {
      // The SDK ForumChannel constructor defaults absent flags to zero and
      // caches old tags on partial channel updates. Its object alone cannot
      // establish that the fresh REST payload actually contained both fields.
      const wire = await client.rest.get(channelRoute(channelId));
      if (!wire || typeof wire !== 'object' || Array.isArray(wire)) {
        throw new PrivateRpcError('invalid_request');
      }
      const raw = wire as Record<string, unknown>;
      if (raw.id !== channelId || raw.guild_id !== guildId ||
          raw.type !== channel.type) {
        throw new PrivateRpcError('invalid_request');
      }
      observed = forumTags(raw);
    }
    // No operator-pinned live server build has proven the end-to-end physical
    // write contracts. SDK method existence is not permission to POST.
    return {channel_id: channelId, guild_id: guildId,
      forum_posts: false, message_threads: false, thread_replies: false,
      attachments: false, message_edit_delete: false,
      webhook_send: false, webhook_edit_delete: false,
      tag_requirements_verified: observed.verified,
      requires_tag: observed.requiresTag, available_tags: observed.availableTags};
  };
}
