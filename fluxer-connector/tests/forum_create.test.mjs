/** Forum post RPC scenarios through the real authenticated Node listener.
 * Only the upstream SDK REST edge is replaced by a stateful test provider.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {PrivateRpcService} from '../src/transport.ts';
import {createSdkForumCreateHandler} from '../src/forum_create.ts';

/** A forum returned by the pinned SDK channel manager. */
function forum() {
  return {id:'forum-1',guildId:'guild-1',type:15,name:'News',
    isGuild:()=>true,isForum:()=>true,isThread:()=>false,
    isText:()=>false,isAnnouncement:()=>false};
}

/** Simulate the SDK's REST methods with external side effects stored independently. */
async function fixture({response,flags=16,tags=[{id:'tag-a',moderated:false}], botId='bot-1'}={}) {
  const remote=[];
  const wire={id:'forum-1',guild_id:'guild-1',type:15,flags,available_tags:tags};
  const member={id:'bot-1',guild:{id:'guild-1'},roles:{roleIds:['role-1']},
    permissionsIn:()=>({has:(flag)=>['ViewChannel','SendMessages'].includes(flag)})};
  const guild={id:'guild-1',fetchRoles:async()=>[{id:'guild-1'},{id:'role-1'}],
    members:{fetch:async()=>member}};
  const client={user:botId?{id:botId,bot:true}:null,
    guilds:{fetch:async()=>guild},
    channels:{async fetch(id,options){
      assert.equal(options.force,true);
      if(id!=='forum-1') throw Error('not found');
      return forum();
    }},
    rest:{
      async get(route){assert.equal(route,'/channels/forum-1');return wire;},
      async post(route,options){
        remote.push({route,options});
        if(response instanceof Error)throw response;
        return response??{id:'thread-9',guild_id:'guild-1',parent_id:'forum-1',type:11,
          message:{id:'starter-91',channel_id:'thread-9',guild_id:'guild-1',
            author:{id:'bot-1',bot:true},webhook_id:null,content:options.body.message.content}};
      },
    },
  };
  const rpc=new PrivateRpcService({connectorKey:'fx',transportSecret:'private-secret',
    handler:createSdkForumCreateHandler(client,id=>`/channels/${id}`,
      id=>`/channels/${id}/threads`)});
  const port=await rpc.start();
  const request=async(args)=>{
    const resp=await fetch(`http://127.0.0.1:${port}/rpc`,{
      method:'POST',headers:{Authorization:'Bearer private-secret'},
      body:JSON.stringify({connector_key:'fx',operation:'create_forum_thread',args}),
    });
    return {status:resp.status,body:await resp.json()};
  };
  return {request,remote,wire,close:()=>rpc.close()};
}

const attempt={forum_id:'forum-1',guild_id:'guild-1',title:'New topic',
  content:{body:'Hello from AP',format:'plain'},allowed_mentions:{parse:[]},
  applied_tags:['tag-a'],attempt_key:'attempt-1',delivery_mode:'bot'};

test('one authenticated RPC creates a forum thread and preserves two server-proven physical IDs',async()=>{
  const h=await fixture();
  try {
    const value=await h.request(attempt);
    assert.equal(value.status,200);
    assert.deepEqual(value.body.result,{forum_id:'forum-1',guild_id:'guild-1',
      thread_id:'thread-9',starter_id:'starter-91',delivery_mode:'bot'});
    assert.equal(h.remote.length,1);
    assert.deepEqual(h.remote[0],{route:'/channels/forum-1/threads',options:{auth:true,
      body:{name:'New topic',type:11,message:{content:'Hello from AP',
        allowed_mentions:{parse:[]}},applied_tags:['tag-a']}}});
  }finally{await h.close();}
});

test('missing/mismatched starter after remote commit is unknown, never a second POST',async()=>{
  for (const message of [undefined,{id:'starter',channel_id:'other',guild_id:'guild-1',
    author:{id:'bot-1',bot:true},webhook_id:null},
    {id:'starter',channel_id:'thread-9',guild_id:'guild-1',
      author:{id:'foreign-bot',bot:true},webhook_id:null}]) {
    const h=await fixture({response:{id:'thread-9',guild_id:'guild-1',parent_id:'forum-1',
      type:11,message}});
    try {
      const value=await h.request(attempt);
      assert.equal(value.status,422);
      assert.equal(value.body.error.reason,'unknown_delivery');
      assert.equal(h.remote.length,1);
    }finally{await h.close();}
  }
});

test('SDK REST transport failure after possible write is unknown delivery',async()=>{
  const h=await fixture({response:new Error('response lost after commit')});
  try {
    const value=await h.request(attempt);
    assert.equal(value.body.error.reason,'unknown_delivery');
    assert.equal(h.remote.length,1);
  }finally{await h.close();}
});

test('missing tag, foreign scope, oversized title and webhook mode refuse before remote write',async()=>{
  const h=await fixture();
  try {
    for (const input of [
      {...attempt,applied_tags:[]},
      {...attempt,applied_tags:['not-listed']},
      {...attempt,guild_id:'guild-other'},
      {...attempt,title:'x'.repeat(101)},
      {...attempt,delivery_mode:'webhook'},
      {...attempt,allowed_mentions:{parse:['everyone']}},
      {...attempt,unexpected:true},
    ]) {
      const value=await h.request(input);
      assert.notEqual(value.status,200);
    }
    assert.equal(h.remote.length,0);
  }finally{await h.close();}
});

test('incomplete live forum tag metadata and moderator-only tags refuse without POST',async()=>{
  const h=await fixture();
  try {
    h.wire.available_tags=undefined;
    const missing=await h.request(attempt);
    assert.notEqual(missing.status,200);
    h.wire.available_tags=[{id:'tag-a',moderated:true}];
    const moderated=await h.request(attempt);
    assert.notEqual(moderated.status,200);
    assert.equal(h.remote.length,0);
  }finally{await h.close();}
});

test('unverified SDK bot identity prevents even one POST',async()=>{
  const h=await fixture({botId:null});
  try {
    assert.notEqual((await h.request(attempt)).status,200);
    assert.equal(h.remote.length,0);
  }finally{await h.close();}
});
