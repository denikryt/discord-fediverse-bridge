/** Reproduce the measured Fluxer REST shapes through real SDK managers and RPC.
 * Only remote REST and guild ACL reads are replaced; Message construction,
 * send/reply serialization, edit/delete, channel narrowing and HTTP RPC run normally.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {Client, Message} from '@fluxerjs/core';
import {PrivateRpcService} from '../src/transport.ts';
import {createSdkMessageSendHandler} from '../src/message_send.ts';
import {createSdkMessageMutationHandler} from '../src/message_mutations.ts';
import {createSdkForumCreateHandler} from '../src/forum_create.ts';
import {createSdkReadHandler} from '../src/rest_reads.ts';

const GUILD = '1568455252934393856';
const TEXT = '1568455585718861824';
const FORUM = '1568455942150815746';
const BOT = '1568456287421726720';
const THREAD = '1568459770296270848';
const PARENT = '1568457788353413120';
const bot = {id: BOT, username: 'Bridge', discriminator: '0001', bot: true};

/** Match the measured server response: guild and webhook fields are omitted. */
function rawMessage(id, channelId, content, author = bot) {
  return {id, channel_id: channelId, author, content, type: 0, flags: 0,
    timestamp: '2026-11-06T21:00:00Z', edited_timestamp: null,
    pinned: false, attachments: [], embeds: [], stickers: [],
    mentions: [], mention_roles: [], mention_everyone: false, tts: false};
}

/** Keep independently stored raw remote state while the real SDK maps responses. */
async function harness({messageResponse = value => value,
                        forumResponse = value => value} = {}) {
  const client = new Client();
  client.user = client.getOrCreateUser(bot);
  const effects = [];
  const messages = new Map([[PARENT, rawMessage(PARENT, TEXT, 'human parent',
    {id: '1568457659672166400', username: 'Human', bot: false})]]);
  const channels = new Map([
    [TEXT, {id: TEXT, guild_id: GUILD, type: 0, name: 'text'}],
    [FORUM, {id: FORUM, guild_id: GUILD, type: 15, name: 'forum', flags: 16,
      available_tags: [{id: 'tag-a', moderated: false}]}],
  ]);
  // ACL behavior is independently covered elsewhere. Supply fresh role/member
  // observations without replacing any message/channel manager or SDK Message.
  const member = {id: BOT, guild: {id: GUILD}, roles: {roleIds: ['writer']},
    permissionsIn: () => ({has: () => true})};
  client.guilds.fetch = async () => ({id: GUILD,
    fetchRoles: async () => [{id: GUILD}, {id: 'writer'}],
    members: {fetch: async () => member}});
  client.rest.get = async route => {
    const parts = route.split('/');
    if (parts.length === 3 && channels.has(parts[2])) {
      return structuredClone(channels.get(parts[2]));
    }
    if (parts.length === 5 && parts[3] === 'messages') {
      const value = messages.get(parts[4]);
      if (value) return structuredClone(value);
    }
    throw Object.assign(Error('remote resource missing'), {statusCode: 404});
  };
  client.rest.post = async (route, options) => {
    effects.push({method: 'POST', route, body: structuredClone(options.body)});
    if (route === `/channels/${FORUM}/threads`) {
      const thread = {id: THREAD, guild_id: GUILD, parent_id: FORUM, type: 11,
        name: options.body.name, thread_metadata: {archived: false, locked: false,
          auto_archive_duration: 1440, archive_timestamp: '2026-11-06T21:00:00Z'}};
      channels.set(THREAD, thread);
      const starter = rawMessage(THREAD, THREAD, options.body.message.content);
      messages.set(starter.id, starter);
      return forumResponse({...structuredClone(thread), message: structuredClone(starter)});
    }
    const containerId = route.split('/')[2];
    const id = String(1568459769398689792n + BigInt(effects.length));
    const message = rawMessage(id, containerId, options.body.content);
    if (options.body.message_reference) {
      message.type = 19;
      message.message_reference = structuredClone(options.body.message_reference);
    }
    messages.set(id, message);
    return messageResponse(structuredClone(message));
  };
  client.rest.patch = async (route, options) => {
    effects.push({method: 'PATCH', route, body: structuredClone(options.body)});
    const message = messages.get(route.split('/')[4]);
    message.content = options.body.content;
    return messageResponse(structuredClone(message));
  };
  client.rest.delete = async route => {
    effects.push({method: 'DELETE', route});
    messages.delete(route.split('/')[4]);
  };
  const send = createSdkMessageSendHandler(client);
  const mutate = createSdkMessageMutationHandler(client);
  const create = createSdkForumCreateHandler(client, id => `/channels/${id}`,
    id => `/channels/${id}/threads`);
  const read = createSdkReadHandler(client);
  const service = new PrivateRpcService({connectorKey: 'fx', transportSecret: 'private-secret',
    handler: (operation, args) => operation === 'send_message' ? send(operation, args)
      : operation === 'create_forum_thread' ? create(operation, args)
      : operation === 'fetch_message' ? read(operation, args) : mutate(operation, args)});
  const port = await service.start();
  return {client, messages, channels, effects,
    async request(operation, args) {
      const response = await fetch(`http://127.0.0.1:${port}/rpc`, {
        method: 'POST', headers: {authorization: 'Bearer private-secret'},
        body: JSON.stringify({connector_key: 'fx', operation, args}),
      });
      return {status: response.status, ...await response.json()};
    },
    async close() { await service.close(); await client.destroy(); },
  };
}

const sendArgs = {container_id: TEXT, guild_id: GUILD, reply_to_message_id: null,
  content: {body: '@everyone safe text', format: 'plain'}, allowed_mentions: {parse: []},
  attempt_key: 'measured-rest-shape', delivery_mode: 'bot'};
const forumArgs = {forum_id: FORUM, guild_id: GUILD, title: 'New topic',
  content: {body: 'Forum starter', format: 'plain'}, allowed_mentions: {parse: []},
  attempt_key: 'measured-forum-shape', delivery_mode: 'bot', applied_tags: ['tag-a']};

test('a full REST read with omitted guild remains a scoped human observation without writes', async () => {
  const h = await harness();
  try {
    const result = await h.request('fetch_message', {container_id: TEXT, id: PARENT});
    assert.equal(result.status, 200);
    assert.equal(result.result.author.kind, 'human');
    assert.equal(result.result.content, 'human parent');
    assert.equal(result.result.container_id, TEXT);
    assert.equal(h.effects.length, 0);
  } finally { await h.close(); }
});

test('measured message REST omission yields one SDK send receipt and scoped read', async () => {
  const h = await harness();
  try {
    const sent = await h.request('send_message', sendArgs);
    assert.equal(sent.status, 200);
    const id = sent.result.message_id;
    const sdkMessage = await h.client.channels.fetchMessage(TEXT, id);
    assert.ok(sdkMessage instanceof Message);
    assert.equal(sdkMessage.guildId, null);
    assert.equal(sdkMessage.webhookId, null);
    const read = await h.request('fetch_message', {container_id: TEXT, id});
    assert.equal(read.status, 200);
    assert.equal(read.result.content, sendArgs.content.body);
    assert.equal(read.result.author.kind, 'bot');
    assert.equal(read.result.webhook_id, null);
    assert.equal(h.effects.length, 1);
    assert.deepEqual(h.effects[0].body.allowed_mentions, {parse: [], replied_user: false});
    assert.equal(h.messages.get(id).content, sendArgs.content.body);
  } finally { await h.close(); }
});

test('a real SDK parent with null guild yields an exact reply rather than a flat send', async () => {
  const h = await harness();
  try {
    const result = await h.request('send_message', {...sendArgs, reply_to_message_id: PARENT});
    assert.equal(result.status, 200);
    const physical = h.messages.get(result.result.message_id);
    assert.deepEqual(physical.message_reference,
      {channel_id: TEXT, message_id: PARENT, guild_id: GUILD});
    assert.equal(h.effects.length, 1);
    assert.deepEqual(h.effects[0].body.allowed_mentions, {parse: [], replied_user: false});
  } finally { await h.close(); }
});

test('real SDK messages without guild support owned edit/delete and preserve a human sibling', async () => {
  const h = await harness();
  try {
    const id = '1568472158445764608';
    h.messages.set(id, rawMessage(id, TEXT, 'owned original'));
    const ref = {container_id: TEXT, message_id: id, bot_id: BOT};
    const updated = await h.request('edit_message', {...ref,
      content: {body: 'updated', format: 'plain'}, allowed_mentions: {parse: []}});
    assert.equal(updated.status, 200);
    assert.equal(h.messages.get(id).content, 'updated');
    assert.equal((await h.request('delete_message', ref)).status, 200);
    assert.equal(h.messages.has(id), false);
    assert.equal(h.messages.get(PARENT).content, 'human parent');
    assert.deepEqual(h.effects.map(effect => effect.method), ['PATCH', 'DELETE']);
  } finally { await h.close(); }
});

test('measured forum response preserves equal starter/thread IDs and supports a nested reply', async () => {
  const h = await harness();
  try {
    const created = await h.request('create_forum_thread', forumArgs);
    assert.equal(created.status, 200);
    assert.equal(created.result.thread_id, THREAD);
    assert.equal(created.result.starter_id, THREAD);
    const read = await h.request('fetch_message', {container_id: THREAD, id: THREAD});
    assert.equal(read.status, 200);
    assert.equal(read.result.content, 'Forum starter');
    const reply = await h.request('send_message', {...sendArgs,
      container_id: THREAD, reply_to_message_id: THREAD});
    assert.equal(reply.status, 200);
    assert.deepEqual(h.messages.get(reply.result.message_id).message_reference,
      {channel_id: THREAD, message_id: THREAD, guild_id: GUILD});
    assert.equal(h.effects.length, 2);
  } finally { await h.close(); }
});

test('missing message guild never excuses conflicting scope, webhook, or foreign ownership', async () => {
  const h = await harness();
  try {
    const id = '1568472158445764608';
    for (const overrides of [
      {channel_id: FORUM}, {guild_id: 'foreign-guild'}, {guild_id: ''},
      {webhook_id: 'hook-1'}, {author: {...bot, id: 'foreign-bot'}},
    ]) {
      h.messages.set(id, {...rawMessage(id, TEXT, 'untouched'), ...overrides});
      const ref = {container_id: TEXT, message_id: id, bot_id: BOT};
      assert.notEqual((await h.request('edit_message', {...ref,
        content: {body: 'changed', format: 'plain'}, allowed_mentions: {parse: []}})).status, 200);
      assert.notEqual((await h.request('delete_message', ref)).status, 200);
      assert.equal(h.messages.get(id).content, 'untouched');
    }
    for (const overrides of [{channel_id: FORUM}, {guild_id: 'foreign-guild'}]) {
      h.messages.set(PARENT, {...rawMessage(PARENT, TEXT, 'parent'), ...overrides});
      assert.notEqual((await h.request('send_message', {...sendArgs,
        reply_to_message_id: PARENT})).status, 200);
      assert.notEqual((await h.request('fetch_message', {container_id: TEXT, id: PARENT})).status, 200);
    }
    assert.equal(h.effects.length, 0);
  } finally { await h.close(); }
});

test('contradictory message receipt after a real SDK write remains unknown with one physical attempt', async () => {
  for (const overrides of [{guild_id: 'foreign-guild'}, {channel_id: FORUM},
    {webhook_id: 'hook-1'}, {author: {...bot, id: 'foreign-bot'}}, {content: 'wrong'}]) {
    const h = await harness({messageResponse: message => ({...message, ...overrides})});
    try {
      const result = await h.request('send_message', sendArgs);
      assert.equal(result.error.reason, 'unknown_delivery');
      assert.equal(h.effects.length, 1);
      assert.equal([...h.messages.values()].filter(m => m.content === sendArgs.content.body).length, 1);
    } finally { await h.close(); }
  }
});

test('contradictory forum starter or thread is unknown after one POST and never replaced', async () => {
  for (const change of [
    value => { value.guild_id = 'foreign-guild'; },
    value => { value.parent_id = TEXT; },
    value => { value.message.guild_id = 'foreign-guild'; },
    value => { value.message.channel_id = TEXT; },
    value => { value.message.webhook_id = 'hook-1'; },
    value => { value.message.author.id = 'foreign-bot'; },
    value => { value.message.content = 'wrong'; },
    value => { delete value.message; },
  ]) {
    const h = await harness({forumResponse: value => { change(value); return value; }});
    try {
      const result = await h.request('create_forum_thread', forumArgs);
      assert.equal(result.error.reason, 'unknown_delivery');
      assert.equal(h.effects.length, 1);
      assert.equal(h.messages.get(THREAD).content, forumArgs.content.body);
    } finally { await h.close(); }
  }
});
