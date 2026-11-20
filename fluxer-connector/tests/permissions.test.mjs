/** Operator permission probes through real RPC and a controlled SDK boundary. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {PrivateRpcService} from '../src/transport.ts';
import {createSdkPermissionHandler} from '../src/permissions.ts';

/** Model the authoritative SDK channel type guards and scoped parent. */
function channel(id, guild='guild-1', parent=null) {
  return {id, guildId:guild, parentId:parent, isGuild:()=>true,
    isThread:()=>id.startsWith('thread-'), isForum:()=>id.startsWith('forum-')};
}

/** Start the real RPC with a fake Fluxer guild/member/roles source. */
async function harness({missingRole=false, parentDeny=false, parentNoSend=false,
                         offline=false, threadSend=false, noHistory=false}={}) {
  const requests=[];
  const channels=new Map([['forum-1',channel('forum-1')],
    ['thread-1',channel('thread-1','guild-1','forum-1')]]);
  const member={id:'bot-id',guild:{id:'guild-1'},roles:{roleIds:['role-1']},
    permissionsIn(ch) {
      requests.push(['permissionsIn',ch.id]);
      const granted=ch.id==='forum-1' && parentDeny
        ? new Set(['ReadMessageHistory'])
        : new Set(['ViewChannel',...(!noHistory?['ReadMessageHistory']:[]),
                   ...(!parentNoSend || ch.isThread()?['SendMessages']:[]),
                   ...(ch.isThread() && threadSend?[1n << 38n]:[])]);
      return {has:(name)=>granted.has(name)};
    }};
  const guild={id:'guild-1',roles:new Map([['guild-1',{}],['role-1',{}]]),
    async fetchRoles() {
      requests.push(['roles']);
      return missingRole?[{id:'guild-1'}]:[{id:'guild-1'},{id:'role-1'}];
    },
    members:{async fetch(id, options) {
      requests.push(['member',id,options]);
      if(offline)throw new Error('SDK transport unavailable');
      return member;
    }},
  };
  const client={user:{id:'bot-id',bot:true},
    channels:{async fetch(id, options) {
      requests.push(['channel',id,options]);
      return channels.get(id);
    }},
    guilds:{async fetch(id, options) {
      requests.push(['guild',id,options]);
      return id==='guild-1'?guild:undefined;
    }}};
  const service=new PrivateRpcService({connectorKey:'fx-acl',transportSecret:'acl-secret',
    handler:createSdkPermissionHandler(client)});
  const port=await service.start();
  const check=async(args)=>{
    const response=await fetch(`http://127.0.0.1:${port}/rpc`,{
      method:'POST',headers:{authorization:'Bearer acl-secret'},
      body:JSON.stringify({connector_key:'fx-acl',operation:'check_permissions',args}),
    });
    return {status:response.status,body:await response.json()};
  };
  return {check,requests,stop:()=>service.close()};
}

test('operator checks current bot ACL: only evidenced view/history/send are allowed',async()=>{
  const h=await harness();
  try {
    const result=await h.check({bot_id:'bot-id',channel_id:'forum-1',
      actions:['view_channel','read_history','send_message','create_forum_post',
        'start_message_thread','edit_message','delete_message']});
    assert.equal(result.status,200);
    assert.deepEqual(result.body.result,{
      bot_id:'bot-id',channel_id:'forum-1',guild_id:'guild-1',
      view_channel:'allowed',read_history:'allowed',send_message:'allowed',
      create_forum_post:'allowed',start_message_thread:'unknown',
      edit_message:'allowed',delete_message:'allowed',
    });
    assert.ok(h.requests.some(([op,id,opts])=>op==='channel' && id==='forum-1' && opts.force));
    assert.ok(h.requests.some(([op,id,opts])=>op==='member' && id==='bot-id' && opts.force));
    assert.ok(h.requests.some(([op])=>op==='roles'));
  } finally {await h.stop();}
});

test('thread permissions require parent access; revoked parent denies send',async()=>{
  const h=await harness({parentDeny:true});
  try {
    const result=await h.check({bot_id:'bot-id',channel_id:'thread-1',
      actions:['view_channel','read_history','send_message']});
    assert.equal(result.status,200);
    assert.equal(result.body.result.view_channel,'denied');
    assert.equal(result.body.result.read_history,'denied');
    assert.equal(result.body.result.send_message,'denied');
    assert.deepEqual(h.requests.filter(([op])=>op==='permissionsIn').map(x=>x[1]),
      ['thread-1','forum-1']);
  } finally {await h.stop();}
});

test('thread SEND_MESSAGES flag does not prove SEND_MESSAGES_IN_THREADS',async()=>{
  const h=await harness();
  try {
    const result=await h.check({bot_id:'bot-id',channel_id:'thread-1',
      actions:['send_message','create_forum_post','start_message_thread']});
    assert.equal(result.status,200);
    assert.equal(result.body.result.send_message,'denied');
    assert.equal(result.body.result.create_forum_post,'denied');
    assert.equal(result.body.result.start_message_thread,'unknown');
  }finally{await h.stop();}
});

test('fresh thread bit 38 allows sending despite absence of parent ordinary send rights',async()=>{
  const h=await harness({threadSend:true,parentNoSend:true});
  try {
    const result=await h.check({bot_id:'bot-id',channel_id:'thread-1',
      actions:['send_message','create_forum_post','edit_message','delete_message']});
    assert.equal(result.status,200);
    assert.equal(result.body.result.send_message,'allowed');
    assert.equal(result.body.result.create_forum_post,'denied');
    assert.equal(result.body.result.edit_message,'allowed');
    assert.equal(result.body.result.delete_message,'allowed');
  }finally{await h.stop();}
});

test('bot-owned mutations require current history permission before message ownership lookup',async()=>{
  const h=await harness({noHistory:true});
  try {
    const result=await h.check({bot_id:'bot-id',channel_id:'forum-1',
      actions:['edit_message','delete_message','read_history']});
    assert.equal(result.status,200);
    assert.equal(result.body.result.edit_message,'denied');
    assert.equal(result.body.result.delete_message,'denied');
    assert.equal(result.body.result.read_history,'denied');
  }finally{await h.stop();}
});

test('forum POST is denied with missing necessary view or send rights',async()=>{
  const h=await harness({parentDeny:true});
  try {
    const result=await h.check({bot_id:'bot-id',channel_id:'forum-1',
      actions:['create_forum_post','send_message']});
    assert.equal(result.status,200);
    assert.equal(result.body.result.create_forum_post,'denied');
    assert.equal(result.body.result.send_message,'denied');
  }finally{await h.stop();}
});

test('missing fresh role cannot turn stale cached permission into allowed',async()=>{
  const h=await harness({missingRole:true});
  try {
    const result=await h.check({bot_id:'bot-id',channel_id:'forum-1',
      actions:['view_channel','send_message']});
    assert.equal(result.status,200);
    assert.equal(result.body.result.view_channel,'unknown');
    assert.equal(result.body.result.send_message,'unknown');
  } finally {await h.stop();}
});

test('wrong bot principal and malformed permissions fail before SDK fetch',async()=>{
  const h=await harness();
  try {
    const wrong=await h.check({bot_id:'foreign-bot',channel_id:'forum-1',
      actions:['view_channel']});
    assert.equal(wrong.status,200);
    assert.equal(wrong.body.result.view_channel,'unknown');
    assert.equal(h.requests.length,0);
    const malformed=await h.check({bot_id:'bot-id',channel_id:'forum-1',actions:['sudo']});
    assert.equal(malformed.status,422);
    assert.equal(malformed.body.error.reason,'invalid_request');
    assert.equal(h.requests.length,0);
  } finally {await h.stop();}
});

test('SDK ACL outage returns unavailable, not an invented allow',async()=>{
  const h=await harness({offline:true});
  try {
    const result=await h.check({bot_id:'bot-id',channel_id:'forum-1',
      actions:['view_channel']});
    assert.equal(result.status,503);
    assert.equal(result.body.error.reason,'unavailable');
  } finally {await h.stop();}
});
