/** One SDK v3.3.0 object → Python neutral DTO conversion boundary.
 *
 * Fluxer ClientEvents carry camelCase Channel/ThreadChannel, Message and
 * PartialMessage objects, NOT raw THREAD_* and MESSAGE_* Gateway payloads.
 * Ownership and canonical publication are checked only on Python/SQLite.
 */
export type SdkEventName = 'threadCreate' | 'threadUpdate' | 'threadDelete' |
  'messageCreate' | 'messageUpdate' | 'messageDelete';
export type NeutralWire = Record<string, unknown> & {type: string; connector_key: string};
export type Observation = {receivedAt?: string; ownedBotId?: string};

function object(value: unknown): Record<string, unknown> {
  /** Do not interpret a missing cached SDK object as a validated platform fact. */
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('invalid Fluxer SDK event');
  }
  return value as Record<string, unknown>;
}
function id(value: unknown): string {
  /** Preserve opaque platform IDs, including non-decimal IDs, without coercion. */
  if (typeof value !== 'string' || !value.trim() || /[\x00-\x1f\x7f]/.test(value)) {
    throw new Error('invalid Fluxer physical identifier');
  }
  return value;
}
function instant(value: unknown): string {
  /** Dates from the hydrated SDK are accepted only when actually observed. */
  if (!(value instanceof Date) || !Number.isFinite(value.getTime())) {
    throw new Error('invalid Fluxer SDK timestamp');
  }
  return value.toISOString();
}
function maybeGuild(data: Record<string, unknown>): string | undefined {
  return data.guildId == null ? undefined : id(data.guildId);
}
function threadRef(connector: string, data: Record<string, unknown>) {
  // SDK thread channels are 10/11/12; a forum itself is channel type 15/16.
  if (![10, 11, 12].includes(data.type as number)) {
    throw new Error('unsupported Fluxer thread type');
  }
  const parent = id(data.parentId);
  const guild = id(data.guildId);
  return {channel: {connector_key: connector, external_channel_id: parent,
    kind: 'unknown', external_guild_id: guild}, external_thread_id: id(data.id)};
}
function messageRef(connector: string, data: Record<string, unknown>) {
  return {connector_key: connector, external_container_id: id(data.channelId),
    external_message_id: id(data.id)};
}
function author(connector: string, data: Record<string, unknown>, botId?: string) {
  const who = object(data.author);
  const actor = id(who.id);
  if (typeof who.bot !== 'boolean') throw new Error('unverified Fluxer author bot flag');
  const webhook = data.webhookId != null;
  if (webhook) id(data.webhookId);
  const kind = webhook ? 'webhook' : who.bot ? 'bot' : 'human';
  return {connector_key: connector, external_actor_id: actor, kind,
    display_name: typeof who.username === 'string' ? who.username : '',
    is_bridge_owned: !webhook && who.bot && botId === actor};
}
function attachmentList(input: unknown): Record<string, unknown>[] {
  /** SDK Message.attachments is a Collection, never an API payload array. */
  if (!input || typeof input !== 'object' ||
      typeof (input as {values?: unknown}).values !== 'function') {
    throw new Error('unverified Fluxer attachments');
  }
  const output: Record<string, unknown>[] = [];
  for (const value of (input as Map<string, unknown>).values()) {
    const a = object(value);
    if (typeof a.filename !== 'string' || !a.filename ||
        typeof a.url !== 'string' || !/^https?:\/\//.test(a.url)) {
      throw new Error('unverified Fluxer attachment');
    }
    const item: Record<string, unknown> = {filename: a.filename, url: a.url};
    if (a.contentType != null) {
      if (typeof a.contentType !== 'string') throw new Error('invalid attachment content type');
      item.mime_type = a.contentType;
    }
    if (a.size != null) {
      if (!Number.isSafeInteger(a.size) || (a.size as number) < 0) {
        throw new Error('invalid attachment size');
      }
      item.size = a.size;
    }
    output.push(item);
  }
  return output;
}

/** Convert one published SDK ClientEvent, never a raw Gateway event object.
 *
 * For update events the SDK delivers (old, new); only the new snapshot is
 * authoritative. Missing partial content stays absent, not an empty edit.
 * `null` represents a known non-content/system message, not successful AP work.
 */
export function normalizeSdkEvent(
  connectorKey: string, name: SdkEventName,
  args: readonly unknown[], observation: Observation = {},
): NeutralWire | null {
  const connector = id(connectorKey);
  const newer = name === 'threadUpdate' || name === 'messageUpdate' ? args[1] : args[0];
  const data = object(newer);
  const received_at = observation.receivedAt === undefined
    ? new Date().toISOString() : instant(new Date(observation.receivedAt));
  const base = {connector_key: connector, received_at};
  const guild = maybeGuild(data);
  if (name.startsWith('thread')) {
    const thread = threadRef(connector, data);
    if (name === 'threadCreate') {
      if (typeof data.name !== 'string') throw new Error('Fluxer SDK thread name missing');
      // The SDK may discover an already existing thread via CHANNEL_CREATE.
      // ThreadCreate is not proof of a newly created canonical forum post.
      return {...base, type: 'ThreadCreated', thread, name: data.name,
        ...(guild ? {external_guild_id: guild} : {})};
    }
    return {...base, type: name === 'threadUpdate' ? 'ThreadUpdated' : 'ThreadDeleted',
      thread, ...(name === 'threadUpdate' && typeof data.name === 'string'
        ? {name: data.name} : {}), ...(guild ? {external_guild_id: guild} : {})};
  }
  const message = messageRef(connector, data);
  if (name === 'messageDelete') {
    return {...base, type: 'MessageDeleted', message,
      ...(guild ? {external_guild_id: guild} : {})};
  }
  if (name === 'messageCreate') {
    // Ordinary (0) and reply (19) are user content; system notices are not.
    if (data.type !== 0 && data.type !== 19) return null;
    const reply = data.messageReference == null ? null : object(data.messageReference);
    // Forward references (type 1) are not a thread-local parent reply.
    const reply_to = reply && (reply.type == null || reply.type === 0) &&
      reply.messageId != null && reply.channelId != null
      ? {connector_key: connector, external_container_id: id(reply.channelId),
        external_message_id: id(reply.messageId)} : undefined;
    if (typeof data.content !== 'string') throw new Error('Fluxer SDK message content missing');
    const content: Record<string, unknown> = {body: data.content,
      attachments: attachmentList(data.attachments)};
    return {...base, type: 'MessageCreated', message,
      author: author(connector, data, observation.ownedBotId), content,
      ...(reply_to ? {reply_to} : {}),
      ...(data.createdAt != null ? {created_at: instant(data.createdAt)} : {}),
      ...(guild ? {external_guild_id: guild} : {})};
  }
  // A cached SDK Message and a PartialMessage have different completeness.
  // In particular PartialMessage.content=null does not imply a blank edit.
  const contents = data.content == null ? undefined : {body: data.content};
  if (contents && typeof contents.body !== 'string') {
    throw new Error('invalid Fluxer SDK partial content');
  }
  return {...base, type: 'MessageUpdated', message,
    ...(contents ? {content: contents} : {}),
    ...(data.editedAt != null ? {edited_at: instant(data.editedAt)} : {}),
    ...(data.author != null ? {author: author(connector, data, observation.ownedBotId)} : {}),
    // PartialMessage has no attachment field. Do not infer changes to media.
    ...(data.attachments != null ? {attachments: attachmentList(data.attachments)} : {}),
    ...(guild ? {external_guild_id: guild} : {})};
}
