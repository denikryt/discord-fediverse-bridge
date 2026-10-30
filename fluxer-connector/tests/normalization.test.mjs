/** Versioned public Fluxer Gateway payload -> neutral Python DTO contract. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {normalizeGatewayEvent} from '../src/normalization.ts';

const key = 'fluxer-internal';
const receivedAt = '2026-10-27T12:00:00.000Z';
const source = {receivedAt};
const thread = {id: 'thread-11', type: 11, guild_id: 'guild-1',
  parent_id: 'forum-5', owner_id: 'user-2', name: 'Release checklist', newly_created: true};
const message = {id: 'msg-99', channel_id: 'thread-11', guild_id: 'guild-1',
  author: {id: 'user-2', username: 'someone', bot: false}, content: 'hello',
  type: 0, timestamp: '2026-10-27T11:59:59.000Z'};

test('THREAD_CREATE uses parent and observed IDs without inventing a forum starter', () => {
  const wire = normalizeGatewayEvent(key, 'THREAD_CREATE', thread, source);
  assert.deepEqual(wire, {type: 'ThreadCreated', connector_key: key,
    thread: {channel: {connector_key: key, external_channel_id: 'forum-5',
      kind: 'unknown', external_guild_id: 'guild-1'}, external_thread_id: 'thread-11'},
    name: 'Release checklist', newly_created: true, external_guild_id: 'guild-1',
    received_at: receivedAt});
  assert.equal('starter' in wire, false);
  assert.equal('owner' in wire, false);
});

test('THREAD_UPDATE and THREAD_DELETE keep only observed metadata', () => {
  const update = normalizeGatewayEvent(key, 'THREAD_UPDATE', {...thread, name: 'Renamed'}, source);
  const deletion = normalizeGatewayEvent(key, 'THREAD_DELETE',
    {id: 'thread-11', guild_id: 'guild-1', parent_id: 'forum-5', type: 11}, source);
  assert.equal(update.type, 'ThreadUpdated');
  assert.equal(update.name, 'Renamed');
  assert.equal(deletion.type, 'ThreadDeleted');
  assert.equal('name' in deletion, false);
  assert.equal('starter' in deletion, false);
});

test('MESSAGE_CREATE preserves true timestamp and authenticated bot echo scope', () => {
  const wire = normalizeGatewayEvent(key, 'MESSAGE_CREATE', message, source);
  assert.equal(wire.type, 'MessageCreated');
  assert.deepEqual(wire.message, {connector_key:key,external_container_id:'thread-11',
    external_message_id:'msg-99'});
  assert.deepEqual(wire.author, {connector_key:key,external_actor_id:'user-2',
    kind:'human',display_name:'someone',is_bridge_owned:false});
  assert.deepEqual(wire.content, {body:'hello'});
  assert.equal(wire.created_at, message.timestamp);
  assert.equal('thread_context' in wire, false);
  const echoed = normalizeGatewayEvent(key,'MESSAGE_CREATE',{...message,
    author:{id:'bot-1',username:'bridge',bot:true}}, {...source, ownedBotId:'bot-1'});
  assert.equal(echoed.author.is_bridge_owned,true);
  const webhook = normalizeGatewayEvent(key,'MESSAGE_CREATE',{...message,
    webhook_id:'hook-1',author:{id:'bot-1',username:'Display',bot:true}},
    {...source,ownedBotId:'bot-1'});
  assert.equal(webhook.author.kind,'webhook');
  assert.equal(webhook.author.is_bridge_owned,false);
});

test('MESSAGE_UPDATE supports uncached ID-only and does not change missing to empty', () => {
  const idOnly = {id:'msg-99',channel_id:'thread-11'};
  const wire=normalizeGatewayEvent(key,'MESSAGE_UPDATE',idOnly,source);
  assert.equal(wire.type,'MessageUpdated');
  assert.equal(wire.content,undefined);
  assert.equal(wire.parent_channel,undefined);
  assert.equal(wire.external_guild_id,undefined);
  assert.deepEqual(normalizeGatewayEvent(key,'MESSAGE_UPDATE',
    {...idOnly,content:''},source).content,{body:''});
});

test('MESSAGE_DELETE without author/parent is a valid ID-only deletion', () => {
  const wire=normalizeGatewayEvent(key,'MESSAGE_DELETE',
    {id:'message-1',channel_id:'thread-11'},source);
  assert.deepEqual(wire, {type:'MessageDeleted',connector_key:key,
    message:{connector_key:key,external_container_id:'thread-11',
      external_message_id:'message-1'},received_at:receivedAt});
});

test('malformed events and unresolved placement never synthesize identity', () => {
  for (const [kind,payload] of [
    ['THREAD_CREATE',{...thread,parent_id:null}],
    ['THREAD_DELETE',{id:'x',parent_id:'c'}],
    ['MESSAGE_DELETE',{id:'x'}],
    ['MESSAGE_CREATE',{...message,content:undefined}],
    ['MESSAGE_CREATE',{...message,author:{username:'anonymous'}}],
  ]) {
    assert.throws(()=>normalizeGatewayEvent(key,kind,payload,source));
  }
  assert.equal(normalizeGatewayEvent(key,'GUILD_CREATE',{},source),null);
  assert.equal(normalizeGatewayEvent(key,'MESSAGE_CREATE',{...message,type:7},source),null);
  assert.throws(()=>normalizeGatewayEvent('other','MESSAGE_DELETE',
    {id:'x',channel_id:'y'}, {receivedAt:'2026-10-27'}));
});

test('native session sequence and epoch never appear in DTO wire', () => {
  const wire=normalizeGatewayEvent(key,'MESSAGE_DELETE',
    {id:'x',channel_id:'thread-11',sequence:333,epoch:'reconnected'},source);
  assert.equal('epoch' in wire,false);
  assert.equal('sequence' in wire,false);
  assert.equal('thread_context' in wire,false);
});

test('unknown thread type cannot be misclassified as a supported forum thread', () => {
  assert.throws(() => normalizeGatewayEvent(key,'THREAD_CREATE',
    {...thread,type:15},source));
  assert.throws(() => normalizeGatewayEvent(key,'THREAD_DELETE',
    {...thread,type:undefined},source));
});

test('native reply and attachments keep exact container and never silently drop media', () => {
  const wire = normalizeGatewayEvent(key,'MESSAGE_CREATE',{...message,
    message_reference: {message_id: 'parent-1', channel_id:'thread-11'},
    attachments: [{url:'https://files.example.test/image.png',filename:'image.png',size:100}]},source);
  assert.deepEqual(wire.reply_to,{connector_key:key,external_container_id:'thread-11',
    external_message_id:'parent-1'});
  assert.deepEqual(wire.content.attachments,[{url:'https://files.example.test/image.png',
    filename:'image.png',size:100}]);
  assert.throws(() => normalizeGatewayEvent(key,'MESSAGE_CREATE',
    {...message,attachments:[{filename:'lost.png'}]},source));
});
