/** Exercise dormant factories with actual SDK Client and Channel instances. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {Client, Channel, ChannelType} from '@fluxerjs/core';
import {verifySdkWriteContracts} from '../src/sdk_write_contracts.ts';
import {createSdkMessageSendHandler} from '../src/message_send.ts';
import {createSdkMessageMutationHandler} from '../src/message_mutations.ts';

test('official SDK Client binds every isolated executor without opening a session', async () => {
  const client = new Client();
  try {
    verifySdkWriteContracts(client);
    assert.equal(client.user, null);
  } finally {
    await client.destroy();
  }
});

test('real SDK DM Channel is refused before message read or physical write', async () => {
  const client = new Client();
  let writes = 0;
  let reads = 0;
  try {
    // Replace authenticated identity and network methods only. The real SDK
    // channel has no guild fields; both factories must narrow before using them.
    client.user = {id: 'bot-1', bot: true};
    const dm = Channel.createDM(client, {id: '123', type: ChannelType.DM});
    client.channels.fetch = async () => dm;
    client.channels.fetchMessage = async () => { reads++; throw Error('unexpected message read'); };
    client.channels.send = async () => { writes++; throw Error('unexpected POST'); };
    await assert.rejects(createSdkMessageSendHandler(client)('send_message', {
      container_id: '123', guild_id: 'guild-1', reply_to_message_id: null,
      content: {format: 'plain', body: 'hello'}, allowed_mentions: {parse: []},
      attempt_key: 'one-shot', delivery_mode: 'bot',
    }), error => error.reason === 'invalid_request');
    await assert.rejects(createSdkMessageMutationHandler(client)('delete_message', {
      container_id: '123', message_id: '456', bot_id: 'bot-1',
    }), error => error.reason === 'invalid_request');
    assert.equal(reads, 0);
    assert.equal(writes, 0);
  } finally {
    await client.destroy();
  }
});
