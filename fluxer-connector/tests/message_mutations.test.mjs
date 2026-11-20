/** Bot-owned physical mutation scenarios across the real authenticated Node RPC.
 * Only the official SDK's remote REST methods are replaced by a stateful fake.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {PrivateRpcService} from '../src/transport.ts';
import {createSdkMessageMutationHandler} from '../src/message_mutations.ts';

/** Supply exact SDK-like channels and independently mutable remote messages. */
async function fixture({remoteFailure, permissionDenied = false, botId = 'bot-1'} = {}) {
  const calls = [];
  const messages = new Map();
  const original = {
    id: 'msg-1', channelId: 'thread-1', guildId: 'guild-1', content: 'old',
    author: {id: 'bot-1', bot: true, username: 'Bridge'}, webhookId: null,
  };
  const foreign = {...original, id: 'foreign', content: 'foreign',
    author: {id: 'other-bot', bot: true, username: 'Foreign'}};
  const hook = {...original, id: 'hook', content: 'hook', webhookId: 'hook-1'};
  for (const m of [original, foreign, hook]) messages.set(m.id, {...m});
  const channel = (id, kind = 'thread') => ({
    id, guildId: 'guild-1', parentId: kind === 'thread' ? 'forum-1' : null,
    isGuild: () => true, isThread: () => kind === 'thread',
    isText: () => kind === 'text', isAnnouncement: () => false,
    isForum: () => kind === 'forum',
  });
  const member = {id: 'bot-1', guild: {id: 'guild-1'}, roles: {roleIds: ['writer']},
    permissionsIn: () => ({has: flag => !permissionDenied &&
      ['ViewChannel', 'ReadMessageHistory'].includes(flag)})};
  const guild = {id: 'guild-1',
    fetchRoles: async () => [{id: 'guild-1'}, {id: 'writer'}],
    members: {fetch: async () => member}};
  const client = {
    user: botId ? {id: botId, bot: true} : null,
    guilds: {fetch: async () => guild},
    channels: {
      async fetch(id, options) {
        assert.equal(options.force, true);
        if (id === 'thread-1') return channel(id);
        if (id === 'forum-1') return channel(id, 'forum');
        return undefined;
      },
      async fetchMessage(container, id) {
        calls.push({type: 'read', container, id});
        if (remoteFailure?.stage === 'read') throw remoteFailure.error;
        const message = messages.get(id);
        if (!message) throw Object.assign(Error('missing'), {statusCode: 404});
        return {
          ...message,
          async edit(payload) {
            calls.push({type: 'edit', id, payload});
            if (remoteFailure?.stage === 'edit') throw remoteFailure.error;
            message.content = payload.content;
            return remoteFailure?.response ?? {...message};
          },
          async delete() {
            calls.push({type: 'delete', id});
            if (remoteFailure?.stage === 'delete') throw remoteFailure.error;
            messages.delete(id);
          },
        };
      },
    },
  };
  const rpc = new PrivateRpcService({
    connectorKey: 'fluxer-one', transportSecret: 'private-secret',
    handler: createSdkMessageMutationHandler(client),
  });
  const port = await rpc.start();
  const request = async (operation, args, bearer = 'private-secret') => {
    const res = await fetch(`http://127.0.0.1:${port}/rpc`, {
      method: 'POST', headers: {authorization: `Bearer ${bearer}`},
      body: JSON.stringify({connector_key: 'fluxer-one', operation, args}),
    });
    return {status: res.status, body: await res.json()};
  };
  return {client, calls, messages, request, close: () => rpc.close()};
}

const ref = {container_id: 'thread-1', message_id: 'msg-1', bot_id: 'bot-1'};
const edit = {...ref, content: {body: '@everyone safe text', format: 'plain'},
  allowed_mentions: {parse: []}};

/** A known own message changes remotely and its exact physical ACK is returned. */
test('one SDK edit and delete change only the addressed bot-owned message', async () => {
  const h = await fixture();
  try {
    const updated = await h.request('edit_message', edit);
    assert.equal(updated.status, 200);
    assert.deepEqual(updated.body.result, {container_id: 'thread-1', message_id: 'msg-1'});
    assert.equal(h.messages.get('msg-1').content, '@everyone safe text');
    assert.deepEqual(h.calls.filter(c => c.type === 'edit')[0].payload,
      {content: '@everyone safe text', allowedMentions: {parse: []}});
    const deleted = await h.request('delete_message', ref);
    assert.equal(deleted.status, 200);
    assert.equal(h.messages.has('msg-1'), false);
    assert.equal(h.messages.get('foreign').content, 'foreign');
    assert.equal(h.calls.filter(c => c.type === 'delete').length, 1);
  } finally { await h.close(); }
});

test('foreign bot, webhook, wrong container, invalid payload and stale bot deny without write', async () => {
  const h = await fixture();
  try {
    for (const [op, args] of [
      ['delete_message', {...ref, message_id: 'foreign'}],
      ['edit_message', {...edit, message_id: 'hook'}],
      ['delete_message', {...ref, container_id: 'forum-1'}],
      ['edit_message', {...edit, allowed_mentions: {parse: ['everyone']}}],
      ['delete_message', {...ref, unexpected: true}],
      ['edit_message', {...edit, content: {body: 'plain', format: 'markdown'}}],
      ['edit_message', {...edit, bot_id: 'other-bot'}],
    ]) {
      const result = await h.request(op, args);
      assert.notEqual(result.status, 200);
    }
    assert.equal(h.calls.filter(c => c.type !== 'read').length, 0);
    assert.equal(h.messages.get('msg-1').content, 'old');
  } finally { await h.close(); }
  const offline = await fixture({botId: null});
  try {
    assert.notEqual((await offline.request('delete_message', ref)).status, 200);
    assert.equal(offline.calls.length, 0);
  } finally { await offline.close(); }
});

test('current bot channel ACL denial blocks mutation before SDK write', async () => {
  const h = await fixture({permissionDenied: true});
  try {
    const answer = await h.request('edit_message', edit);
    assert.equal(answer.body.error.reason, 'forbidden');
    assert.equal(h.calls.filter(c => c.type === 'edit').length, 0);
  } finally { await h.close(); }
});

test('a mismatched result or lost edit response is unknown, never positive ACK', async () => {
  for (const failed of [
    {stage: 'edit', error: Error('connection dropped after write')},
    {stage: 'edit', response: {id: 'wrong', channelId: 'thread-1', guildId: 'guild-1',
      content: '@everyone safe text', author: {id: 'bot-1', bot: true}, webhookId: null}},
    {stage: 'edit', response: {id: 'msg-1', channelId: 'thread-1', guildId: 'guild-1',
      content: 'truncated', author: {id: 'bot-1', bot: true}, webhookId: null}},
  ]) {
    const h = await fixture({remoteFailure: failed});
    try {
      const answer = await h.request('edit_message', edit);
      assert.equal(answer.body.error.reason, 'unknown_delivery');
      assert.equal(h.calls.filter(c => c.type === 'edit').length, 1);
    } finally { await h.close(); }
  }
});

test('definitive HTTP failures are classified, network uncertainty stays unknown', async () => {
  for (const [code, reason] of [[400, 'invalid_request'], [401, 'forbidden'],
    [403, 'forbidden'], [404, 'not_found'], [429, 'rate_limited']]) {
    const h = await fixture({remoteFailure: {stage: 'edit',
      error: Object.assign(Error('SDK REST error'), {statusCode: code})}});
    try {
      const answer = await h.request('edit_message', edit);
      assert.equal(answer.body.error.reason, reason);
    } finally { await h.close(); }
  }
  const h = await fixture({remoteFailure: {stage: 'delete',
    error: Error('connection lost after remote delete')}});
  try {
    assert.equal((await h.request('delete_message', ref)).body.error.reason, 'unknown_delivery');
    assert.equal(h.calls.filter(c => c.type === 'delete').length, 1);
  } finally { await h.close(); }
});

test('SDK mapped REST read failures retain definite not-found and forbidden status', async () => {
  for (const [error, expected] of [
    [Object.assign(Error('message missing'), {code: 'MESSAGE_NOT_FOUND'}), 'not_found'],
    [Object.assign(Error('read forbidden'), {
      cause: Object.assign(Error('HTTP 403'), {statusCode: 403}),
    }), 'forbidden'],
  ]) {
    const h = await fixture({remoteFailure: {stage: 'read', error}});
    try {
      const answer = await h.request('delete_message', ref);
      assert.equal(answer.body.error.reason, expected);
      assert.equal(h.calls.filter(c => c.type === 'delete').length, 0);
    } finally { await h.close(); }
  }
});

test('private RPC refuses untrusted bearer before touching SDK or remote messages', async () => {
  const h = await fixture();
  try {
    const refused = await h.request('delete_message', ref, 'wrong-secret');
    assert.equal(refused.status, 403);
    assert.equal(h.calls.length, 0);
    assert.equal(h.messages.has('msg-1'), true);
  } finally { await h.close(); }
});
