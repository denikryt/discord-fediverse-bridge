/** Behavioral tests for SDK-backed read RPC over the real private HTTP listener. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {PrivateRpcService} from '../src/transport.ts';
import {createSdkReadHandler} from '../src/rest_reads.ts';

/** A deterministic SDK Channel with the published type-guard methods. */
function channel(id, kind, guildId='guild-a', parentId=null) {
  return {id, guildId, parentId, name:id,
    isForum:()=>kind==='forum', isGuild:()=>true, isThread:()=>kind==='thread',
    isText:()=>kind==='text', isAnnouncement:()=>kind==='announcement'};
}

/** Start a real RPC HTTP listener with a fake external SDK read provider. */
async function harness(channels, messages=new Map()) {
  const fetched=[];
  const client={channels:{
    async fetch(id, options) {
      fetched.push(['channel',id,options]);
      if(!channels.has(id)) throw new Error('unavailable channel');
      return channels.get(id);
    },
    async fetchMessage(container,id) {
      fetched.push(['message',container,id]);
      if(!messages.has(`${container}/${id}`))throw new Error('missing message');
      return messages.get(`${container}/${id}`);
    },
  }};
  const service=new PrivateRpcService({connectorKey:'fx-01',transportSecret:'secret-value',
    handler:createSdkReadHandler(client)});
  const port=await service.start();
  const request=async(operation,args)=>{
    const response=await fetch(`http://127.0.0.1:${port}/rpc`,{
      method:'POST',headers:{authorization:'Bearer secret-value','content-type':'application/json'},
      body:JSON.stringify({connector_key:'fx-01',operation,args}),
    });
    return {status:response.status,body:await response.json()};
  };
  return {request,fetched,stop:()=>service.close()};
}

test('operator fetches forum and its existing thread; only verified parent is used, starter is not invented',async()=>{
  const h=await harness(new Map([
    ['forum',channel('forum','forum')],
    ['thread',{...channel('thread','thread','guild-a','forum'),name:'topic'}],
  ]));
  try {
    const forum=await h.request('fetch_channel',{id:'forum'});
    assert.equal(forum.status,200);
    assert.deepEqual(forum.body.result,{id:'forum',kind:'forum',guild_id:'guild-a',name:'forum'});
    const thread=await h.request('fetch_thread',{id:'thread'});
    assert.equal(thread.status,200);
    assert.deepEqual(thread.body.result,{id:'thread',parent_id:'forum',guild_id:'guild-a',
      name:'topic',kind:'forum',starter_id:null});
    assert.deepEqual(h.fetched,[['channel','forum',{force:true}],
      ['channel','thread',{force:true}],['channel','forum',{force:true}]]);
  } finally {await h.stop();}
});

test('thread with mismatched authoritative parent guild is refused, not relabelled',async()=>{
  const h=await harness(new Map([
    ['forum',channel('forum','forum','guild-b')],
    ['thread',channel('thread','thread','guild-a','forum')],
  ]));
  try {
    const response=await h.request('fetch_thread',{id:'thread'});
    assert.equal(response.status,422);
    assert.equal(response.body.error.reason,'invalid_request');
  } finally {await h.stop();}
});

test('message read uses explicit physical container and returns observed author and dates',async()=>{
  const msg={id:'msg-1',channelId:'thread',guildId:'guild-a',content:'hello',
    author:{id:'user-1',username:'alice',bot:false},webhookId:null,
    createdAt:new Date('2026-10-27T12:00:00Z'),editedAt:null,attachments:new Map()};
  const h=await harness(new Map([['thread',channel('thread','thread','guild-a','forum')]]),
    new Map([['thread/msg-1',msg]]));
  try {
    const response=await h.request('fetch_message',{container_id:'thread',id:'msg-1'});
    assert.equal(response.status,200);
    assert.deepEqual(response.body.result,{id:'msg-1',container_id:'thread',content:'hello',
      author:{id:'user-1',kind:'human',display_name:'alice'},
      created_at:'2026-10-27T12:00:00.000Z',edited_at:null,attachments:[]});
    assert.deepEqual(h.fetched,[['channel','thread',{force:true}],['message','thread','msg-1']]);
  } finally {await h.stop();}
});

test('cross-container message result and untrusted identity fail closed',async()=>{
  const msg={id:'msg-1',channelId:'other',guildId:'guild-a',content:'hello',
    author:{id:'bot-x',username:'bot',bot:true},createdAt:new Date(),attachments:new Map()};
  const h=await harness(new Map([['thread',channel('thread','thread','guild-a','forum')]]),
    new Map([['thread/msg-1',msg]]));
  try {
    const wrong=await h.request('fetch_message',{container_id:'thread',id:'msg-1'});
    assert.equal(wrong.status,422);
    assert.equal(wrong.body.error.reason,'invalid_request');
    const invalid=await h.request('fetch_message',{container_id:'../secret',id:'x'});
    assert.equal(invalid.status,422);
    assert.equal(invalid.body.error.reason,'invalid_request');
  } finally {await h.stop();}
});

test('non-read action does not cross SDK read boundary; no physical writes occur',async()=>{
  const h=await harness(new Map());
  try {
    const rejected=await h.request('create_forum_thread',{id:'forum'});
    assert.equal(rejected.status,422);
    assert.equal(rejected.body.error.reason,'unsupported');
    assert.equal(h.fetched.length,0);
  } finally {await h.stop();}
});
