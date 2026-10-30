/** SDK 3.3.0 public ClientEvents object-shape to Python DTO behavioral contracts. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {normalizeSdkEvent} from '../src/normalization.ts';
const key = 'fluxer-internal';
const at = new Date('2026-10-27T12:00:00.000Z');
const o = {receivedAt: at.toISOString()};
const thread = {id:'thread-11', type:11, parentId:'forum-5', guildId:'guild-1',
  name:'Release checklist'};
const message = {id:'msg-99', channelId:'thread-11', guildId:'guild-1',
  author:{id:'user-2', username:'someone',bot:false},content:'hello',type:0,
  createdAt:new Date('2026-10-27T11:59:59Z'),editedAt:null,attachments:new Map()};

test('SDK ThreadCreate carries the real parent but neither invented starter nor newly_created',()=>{
  const wire = normalizeSdkEvent(key,'threadCreate',[thread],o);
  assert.deepEqual(wire.thread,{channel:{connector_key:key,external_channel_id:'forum-5',
    kind:'unknown',external_guild_id:'guild-1'},external_thread_id:'thread-11'});
  assert.equal(wire.type,'ThreadCreated');
  assert.equal('starter' in wire,false);
  assert.equal('newly_created' in wire,false);
});
test('SDK ThreadUpdate uses second typed argument and ThreadDelete verifies kind',()=>{
  assert.equal(normalizeSdkEvent(key,'threadUpdate',[thread,{...thread,name:'new'}],o).name,'new');
  assert.equal(normalizeSdkEvent(key,'threadDelete',[thread],o).type,'ThreadDeleted');
  assert.throws(()=>normalizeSdkEvent(key,'threadDelete',[{...thread,type:15}],o));
});
test('SDK MessageCreate preserves author, reply, media and true creation date',()=>{
  const msg={...message,messageReference:{channelId:'thread-11',messageId:'parent-1'},
    attachments:new Map([['a',{id:'a',url:'https://files.example.test/a',filename:'a.png',size:5}]])};
  const wire=normalizeSdkEvent(key,'messageCreate',[msg],o);
  assert.equal(wire.type,'MessageCreated');
  assert.equal(wire.created_at,message.createdAt.toISOString());
  assert.equal(wire.reply_to.external_message_id,'parent-1');
  assert.deepEqual(wire.content.attachments,[{filename:'a.png',url:'https://files.example.test/a',size:5}]);
  assert.equal(wire.author.is_bridge_owned,false);
  assert.equal(normalizeSdkEvent(key,'messageCreate',[{...msg,type:7}],o),null);
});
test('bot echo requires exact principal; webhook is never a bridge bot',()=>{
  const m={...message,author:{id:'bot-id',bot:true,username:'bot'}};
  assert.equal(normalizeSdkEvent(key,'messageCreate',[m],{...o,ownedBotId:'bot-id'}).author.is_bridge_owned,true);
  const hook=normalizeSdkEvent(key,'messageCreate',[{...m,webhookId:'w'}],{...o,ownedBotId:'bot-id'});
  assert.equal(hook.author.kind,'webhook');
  assert.equal(hook.author.is_bridge_owned,false);
});
test('uncached SDK partial edit has no fabricated content or timestamp',()=>{
  const part={id:'msg-99',channelId:'thread-11',guildId:null,content:null,partial:true};
  const wire=normalizeSdkEvent(key,'messageUpdate',[null,part],o);
  assert.equal(wire.type,'MessageUpdated');
  assert.equal('content' in wire,false);
  assert.equal('created_at' in wire,false);
  assert.equal('external_guild_id' in wire,false);
  assert.deepEqual(normalizeSdkEvent(key,'messageUpdate',[message,{...part,content:''}],o).content,{body:''});
});
test('ID-only SDK delete needs no author, guild, parent or content',()=>{
  const wire=normalizeSdkEvent(key,'messageDelete',[{id:'x',channelId:'thread-11'}],o);
  assert.deepEqual(wire,{connector_key:key,received_at:at.toISOString(),type:'MessageDeleted',
    message:{connector_key:key,external_container_id:'thread-11',external_message_id:'x'}});
});
test('unknown identities, lost parent and expired attachment URLs fail closed',()=>{
  assert.throws(()=>normalizeSdkEvent(key,'threadCreate',[{...thread,parentId:null}],o));
  assert.throws(()=>normalizeSdkEvent(key,'messageCreate',[{...message,attachments:new Map([['a',{filename:'a',url:null}]])}],o));
  assert.throws(()=>normalizeSdkEvent(key,'messageCreate',[{...message,author:null}],o));
  assert.throws(()=>normalizeSdkEvent(key,'messageDelete',[{id:'x'}],o));
  assert.throws(()=>normalizeSdkEvent(key,'messageUpdate',[null,{...message,editedAt:'invented'}],o));
});
test('SDK-only fields and local shard sequence do not enter neutral wire',()=>{
  const wire=normalizeSdkEvent(key,'threadCreate',[{...thread,epoch:5,sequence:42}],o);
  assert.equal('epoch' in wire,false);
  assert.equal('sequence' in wire,false);
});

test('SDK reply message type 19 remains routable; forwards are not local replies',()=>{
  const reply={...message,type:19,messageReference:{channelId:'thread-11',messageId:'parent'}};
  assert.equal(normalizeSdkEvent(key,'messageCreate',[reply],o).reply_to.external_message_id,'parent');
  const forwarded={...reply,messageReference:{...reply.messageReference,type:1}};
  assert.equal('reply_to' in normalizeSdkEvent(key,'messageCreate',[forwarded],o),false);
});
