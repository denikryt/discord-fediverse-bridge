/** Exercise the actual production RPC composition with a stateful SDK edge.
 * A fake provider replaces only SDK I/O, not dispatch, auth or validators.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {PrivateRpcService} from '../src/transport.ts';
import {createProductionRpcHandler} from '../src/main.ts';

/** Build one trusted bot with SDK-shaped channel and REST operations. */
function provider() {
  const writes=[];
  const saved=new Map();
  const state={allow:true,threadBit:true,rolesKnown:true,closed:false,
    requiresTag:false,tags:[]};
  const acl={has:(flag)=>state.allow && (
    ['ViewChannel','ReadMessageHistory','SendMessages'].includes(flag) ||
      (flag === (1n << 38n) && state.threadBit))};
  const member={id:'bot-1',guild:{id:'guild-1'},roles:{roleIds:['writer']},
    permissionsIn:()=>acl};
  const guild={id:'guild-1',fetchRoles:async()=>(state.rolesKnown ?
    [{id:'guild-1'},{id:'writer'}] : []),
    members:{fetch:async()=>member}};
  const channel=(id)=>({id,guildId:'guild-1',type:id==='forum-1'?15:11,
    parentId:id==='thread-1'?'forum-1':null,archived:state.closed,locked:state.closed,
    isGuild:()=>true,isForum:()=>id==='forum-1',isThread:()=>id==='thread-1',
    isText:()=>false,isAnnouncement:()=>false});
  const wireMessage=(id,container,body)=>({id,channelId:container,guildId:'guild-1',
    content:body,webhookId:null,author:{id:'bot-1',bot:true},messageReference:null,
    attachments:new Map(),async edit(data){writes.push(['edit',id]);
      return {...this,content:data.content};},async delete(){writes.push(['delete',id]);}});
  saved.set('thread-1/message-1',wireMessage('message-1','thread-1','before'));
  const client={user:{id:'bot-1',bot:true},guilds:{fetch:async()=>guild},
    channels:{fetch:async(id)=>channel(id),
      fetchMessage:async(container,id)=>saved.get(`${container}/${id}`),
      send:async(container,options)=>{writes.push(['send',container]);
        const item=wireMessage('message-2',container,options.content);
        item.messageReference=options.replyTo?{
          channelId:container,messageId:options.replyTo.messageId,guildId:'guild-1'}:null;
        saved.set(`${container}/message-2`,item);return item;}},
    rest:{get:async()=>({id:'forum-1',guild_id:'guild-1',type:15,
      flags:state.requiresTag?16:0,available_tags:state.tags}),
      post:async(route,options)=>{writes.push(['create',route]);return {
        id:'thread-3',parent_id:'forum-1',guild_id:'guild-1',type:11,
        message:{id:'starter-3',channel_id:'thread-3',content:options.body.message.content,
          author:{id:'bot-1',bot:true},webhook_id:null}};}},
  };
  return {client,writes,state};
}

const route={channel:(id)=>`/channels/${id}`,channelThreads:(id)=>`/channels/${id}/threads`};

/** Submit to a real authenticated private listener using the main dispatcher. */
async function fixture({verified=true}={}) {
  const state=provider();
  const service=new PrivateRpcService({connectorKey:'fx',transportSecret:'bearer-secret',
    handler:createProductionRpcHandler(state.client,route,{writeContractVerified:verified})});
  const port=await service.start();
  const call=async(operation,args)=>{
    const response=await fetch(`http://127.0.0.1:${port}/rpc`,{method:'POST',
      headers:{authorization:'Bearer bearer-secret'},
      body:JSON.stringify({connector_key:'fx',operation,args})});
    return {status:response.status,body:await response.json()};
  };
  return {...state,call,close:()=>service.close()};
}

test('production dispatcher reaches SDK create/send/own edit/delete exactly once',async()=>{
  const h=await fixture();
  try {
    const create=await h.call('create_forum_thread',{forum_id:'forum-1',guild_id:'guild-1',
      title:'Topic',content:{body:'Body',format:'plain'},allowed_mentions:{parse:[]},
      attempt_key:'create-1',delivery_mode:'bot',applied_tags:[]});
    assert.equal(create.status,200);
    assert.deepEqual([create.body.result.thread_id,create.body.result.starter_id],
      ['thread-3','starter-3']);
    const send=await h.call('send_message',{container_id:'thread-1',guild_id:'guild-1',
      content:{body:'Reply',format:'plain'},reply_to_message_id:'message-1',
      allowed_mentions:{parse:[]},attempt_key:'send-1',delivery_mode:'bot'});
    assert.equal(send.status,200);
    assert.equal(send.body.result.message_id,'message-2');
    const ref={container_id:'thread-1',message_id:'message-1',bot_id:'bot-1'};
    assert.equal((await h.call('edit_message', {...ref,
      content:{body:'after',format:'plain'},allowed_mentions:{parse:[]}})).status,200);
    assert.equal((await h.call('delete_message',ref)).status,200);
    assert.deepEqual(h.writes,[['create','/channels/forum-1/threads'],
      ['send','thread-1'],['edit','message-1'],['delete','message-1']]);
    assert.equal((await h.call('create_reply_thread',{})).status,422);
  }finally{await h.close();}
});

test('production dispatcher cannot claim an unsupported writable surface',async()=>{
  const h=await fixture();
  try {
    const caps=await h.call('capabilities',{channel_id:'forum-1'});
    assert.equal(caps.status,200);
    assert.equal(caps.body.result.forum_posts,true);
    assert.equal(caps.body.result.message_threads,false);
    assert.equal(caps.body.result.webhook_send,false);
    assert.equal((await h.call('create_forum_thread',{forum_id:'forum-1'})).status,422);
    assert.deepEqual(h.writes,[]);
  }finally{await h.close();}
});

// A capability snapshot is a per-channel fact, not a blanket SDK feature flag.
test('closed or unverified thread never advertises writable reply capability',async()=>{
  const h=await fixture();
  try {
    const caps=await h.call('capabilities',{channel_id:'thread-1'});
    assert.equal(caps.status,200);
    assert.equal(caps.body.result.thread_replies,true);
    assert.equal(caps.body.result.message_edit_delete,true);
    assert.equal(caps.body.result.forum_posts,false);
  }finally{await h.close();}
});

/** A production capability flag must track current ACL, never stale channel type. */
test('production capability revocation and restoration are action specific',async()=>{
  const h=await fixture();
  try {
    for (const [allow,expected] of [[true,true],[false,false],[true,true]]) {
      h.state.allow=allow;
      const rights=await h.call('check_permissions',{bot_id:'bot-1',
        channel_id:'forum-1',actions:['create_forum_post']});
      assert.equal(rights.body.result.create_forum_post,allow?'allowed':'denied');
      const caps=await h.call('capabilities',{channel_id:'forum-1'});
      assert.equal(caps.body.result.forum_posts,expected);
      const thread=await h.call('capabilities',{channel_id:'thread-1'});
      assert.equal(thread.body.result.thread_replies,expected);
      assert.equal(thread.body.result.message_edit_delete,expected);
    }
    h.state.allow=false;
    const result=await h.call('create_forum_thread',{forum_id:'forum-1',guild_id:'guild-1',
      title:'Topic',content:{body:'Body',format:'plain'},allowed_mentions:{parse:[]},
      attempt_key:'create-denied',delivery_mode:'bot',applied_tags:[]});
    assert.notEqual(result.status,200);
    assert.deepEqual(h.writes,[]);
    h.state.allow=true;
    h.state.threadBit=false;
    assert.equal((await h.call('capabilities',{channel_id:'thread-1'}))
      .body.result.thread_replies,false);
  }finally{await h.close();}
});

/** Metadata/ACL fakes cannot enable an unverified provider-build contract. */
test('production write flags and physical dispatcher stay disabled before acceptance',async()=>{
  const h=await fixture({verified:false});
  try {
    const caps=await h.call('capabilities',{channel_id:'forum-1'});
    assert.equal(caps.status,200);
    assert.equal(caps.body.result.tag_requirements_verified,true);
    assert.equal(caps.body.result.forum_posts,false);
    const thread=await h.call('capabilities',{channel_id:'thread-1'});
    assert.equal(thread.body.result.thread_replies,false);
    assert.equal(thread.body.result.message_edit_delete,false);
    const denied=await h.call('create_forum_thread',{forum_id:'forum-1'});
    assert.equal(denied.status,422);
    assert.deepEqual(h.writes,[]);
  }finally{await h.close();}
});

/** Unknown ACL, closed threads and unselectable required tags are not writable. */
test('production flags refuse uncertain roles, closed threads and required moderated tags',async()=>{
  const h=await fixture();
  try {
    h.state.rolesKnown=false;
    assert.equal((await h.call('capabilities',{channel_id:'forum-1'}))
      .body.result.forum_posts,false);
    assert.equal((await h.call('capabilities',{channel_id:'thread-1'}))
      .body.result.message_edit_delete,false);
    h.state.rolesKnown=true;
    h.state.closed=true;
    assert.equal((await h.call('capabilities',{channel_id:'thread-1'}))
      .body.result.thread_replies,false);
    h.state.closed=false;
    h.state.requiresTag=true;
    h.state.tags=[{id:'moderated-tag',moderated:true}];
    const blocked=await h.call('capabilities',{channel_id:'forum-1'});
    assert.equal(blocked.body.result.tag_requirements_verified,true);
    assert.equal(blocked.body.result.forum_posts,false);
    h.state.tags=[{id:'open-tag',moderated:false}];
    assert.equal((await h.call('capabilities',{channel_id:'forum-1'}))
      .body.result.forum_posts,true);
    assert.deepEqual(h.writes,[]);
  }finally{await h.close();}
});
