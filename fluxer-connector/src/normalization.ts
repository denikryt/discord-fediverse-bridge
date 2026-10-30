/** Sole Fluxer Gateway payload → connector-scoped Python DTO wire conversion.
 *
 * Event field names are grounded in fluxer_docs gateway/events.md and
 * gateway/threads.md from the supplied Fluxer source tree. No SDK cache, DB
 * forum-ownership guess, Discord integer conversion or replay envelope exists.
 * The Python HTTP admission boundary independently checks the typed result.
 */
export type GatewayEventName = 'THREAD_CREATE' | 'THREAD_UPDATE' | 'THREAD_DELETE' |
  'MESSAGE_CREATE' | 'MESSAGE_UPDATE' | 'MESSAGE_DELETE';
export type NeutralWire = Record<string, unknown> & {type: string; connector_key: string};
export type Observation = {
  receivedAt?: string;
  ownedBotId?: string;
};

function record(value: unknown): Record<string, unknown> {
  /** Reject partial or malformed objects rather than inventing physical facts. */
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('invalid Fluxer Gateway object');
  }
  return value as Record<string, unknown>;
}
function id(value: unknown): string {
  /** External Fluxer IDs stay opaque strings and are never number-cast. */
  if (typeof value !== 'string' || !value.trim() ||
      /[\x00-\x1f\x7f]/.test(value)) {
    throw new Error('invalid Fluxer physical identifier');
  }
  return value;
}
function date(value: unknown): string {
  /** Missing/naive platform timestamps are not replaced by reception time. */
  if (typeof value !== 'string' ||
      !/T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value) ||
      !Number.isFinite(Date.parse(value))) {
    throw new Error('invalid Fluxer Gateway timestamp');
  }
  return value;
}
function optionalGuild(payload: Record<string, unknown>): string | undefined {
  return payload.guild_id == null ? undefined : id(payload.guild_id);
}
function messageRef(connector: string, payload: Record<string, unknown>) {
  return {connector_key: connector, external_container_id: id(payload.channel_id),
    external_message_id: id(payload.id)};
}
function threadRef(connector: string, payload: Record<string, unknown>) {
  // Fluxer documents 10/11/12 as thread types; reject unrelated channels.
  if (![10, 11, 12].includes(payload.type as number)) {
    throw new Error('unsupported Fluxer thread type');
  }
  const guild = id(payload.guild_id);
  const parent = id(payload.parent_id);
  // Parent channel type cannot be inferred from a thread's public type 11.
  return {channel: {connector_key: connector, external_channel_id: parent,
    kind: 'unknown', external_guild_id: guild},
    external_thread_id: id(payload.id)};
}
function author(connector: string, message: Record<string, unknown>, ownedBotId?: string) {
  const raw = record(message.author);
  const actor = id(raw.id);
  if (raw.bot != null && typeof raw.bot !== 'boolean') {
    throw new Error('invalid Fluxer author bot flag');
  }
  const isWebhook = message.webhook_id != null;
  if (isWebhook) id(message.webhook_id);
  const kind = isWebhook ? 'webhook' : raw.bot === true ? 'bot' : 'human';
  return {connector_key: connector, external_actor_id: actor, kind,
    display_name: typeof raw.username === 'string' ? raw.username : '',
    // Display names and webhook identities cannot authenticate a bridge bot.
    is_bridge_owned: !isWebhook && kind === 'bot' && actor === ownedBotId};
}
function attachments(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) throw new Error('invalid Fluxer attachments');
  return value.map(item => {
    const raw = record(item);
    if (typeof raw.url !== 'string' || !/^https?:\/\//.test(raw.url) ||
        typeof raw.filename !== 'string' || !raw.filename.trim()) {
      throw new Error('invalid Fluxer attachment');
    }
    const file: Record<string, unknown> = {url: raw.url, filename: raw.filename};
    if (raw.content_type != null) {
      if (typeof raw.content_type !== 'string') throw new Error('invalid Fluxer attachment type');
      file.mime_type = raw.content_type;
    }
    if (raw.size != null) {
      if (!Number.isSafeInteger(raw.size) || (raw.size as number) < 0) {
        throw new Error('invalid Fluxer attachment size');
      }
      file.size = raw.size;
    }
    return file;
  });
}
function content(payload: Record<string, unknown>) {
  if (typeof payload.content !== 'string') throw new Error('missing Fluxer message content');
  const body: Record<string, unknown> = {body: payload.content};
  if (payload.attachments !== undefined) body.attachments = attachments(payload.attachments);
  return body;
}

/** Map one documented native Gateway event to the strict Python wire DTO.
 *
 * `null` means unrelated/system event, never a fake successful publication.
 * Unverified starter, parent forum and human identity remain absent.
 */
export function normalizeGatewayEvent(
  connectorKey: string, eventName: string, payload: unknown,
  observation: Observation = {},
): NeutralWire | null {
  const connector = id(connectorKey);
  const event = eventName as GatewayEventName;
  if (!['THREAD_CREATE', 'THREAD_UPDATE', 'THREAD_DELETE', 'MESSAGE_CREATE',
        'MESSAGE_UPDATE', 'MESSAGE_DELETE'].includes(event)) return null;
  const data = record(payload);
  const received_at = observation.receivedAt ? date(observation.receivedAt)
    : new Date().toISOString();
  const base = {connector_key: connector, received_at};
  const guild = optionalGuild(data);
  if (event.startsWith('THREAD_')) {
    const thread = threadRef(connector, data);
    if (event === 'THREAD_CREATE') {
      if (typeof data.name !== 'string') throw new Error('Fluxer thread name missing');
      if (data.newly_created !== undefined && typeof data.newly_created !== 'boolean') {
        throw new Error('invalid Fluxer newly_created');
      }
      return {...base, type: 'ThreadCreated', thread, name: data.name,
        ...(data.newly_created !== undefined ? {newly_created: data.newly_created} : {}),
        ...(guild ? {external_guild_id: guild} : {})};
    }
    return {...base, type: event === 'THREAD_UPDATE' ? 'ThreadUpdated' : 'ThreadDeleted',
      thread, ...(event === 'THREAD_UPDATE' && typeof data.name === 'string'
        ? {name: data.name} : {}), ...(guild ? {external_guild_id: guild} : {})};
  }
  const message = messageRef(connector, data);
  if (event === 'MESSAGE_DELETE') {
    // A raw delete without author/content/guild is valid and needs no SDK fetch.
    return {...base, type: 'MessageDeleted', message,
      ...(guild ? {external_guild_id: guild} : {})};
  }
  if (event === 'MESSAGE_CREATE') {
    // Non-default Fluxer system messages are not human-written forum content.
    if (data.type !== undefined && data.type !== 0) return null;
    const reply = data.message_reference == null ? null : record(data.message_reference);
    const reply_to = reply && reply.message_id != null && reply.channel_id != null
      ? {connector_key: connector, external_container_id: id(reply.channel_id),
        external_message_id: id(reply.message_id)} : undefined;
    return {...base, type: 'MessageCreated', message,
      author: author(connector, data, observation.ownedBotId), content: content(data),
      ...(reply_to ? {reply_to} : {}),
      ...(data.timestamp != null ? {created_at: date(data.timestamp)} : {}),
      ...(guild ? {external_guild_id: guild} : {})};
  }
  // Uncached MESSAGE_UPDATE may contain only ID/channel. Absent !== empty.
  return {...base, type: 'MessageUpdated', message,
    ...(data.content != null ? {content: content(data)} : {}),
    ...(data.edited_timestamp != null ? {edited_at: date(data.edited_timestamp)} : {}),
    ...(data.author != null ? {author: author(connector, data, observation.ownedBotId)} : {}),
    ...(data.attachments != null ? {attachments: attachments(data.attachments)} : {}),
    ...(guild ? {external_guild_id: guild} : {})};
}
