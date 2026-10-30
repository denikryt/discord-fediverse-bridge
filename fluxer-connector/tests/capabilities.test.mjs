/** Observed forum policy must not manufacture writable Fluxer capabilities. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {PrivateRpcService} from '../src/transport.ts';
import {createSdkCapabilityHandler} from '../src/capabilities.ts';

/** Drive the actual authenticated RPC listener against an SDK channel snapshot. */
async function harness(channels) {
  const reads=[];
  const client={rest:{async get(route) {
    reads.push(['rest',route]);
    const id=route.split('/').pop();
    const channel=channels.get(id);
    if (!channel) throw new Error('no channel');
    return channel.wire ?? {id:channel.id,guild_id:channel.guildId,type:channel.type,
      flags:channel.flags,available_tags:channel.availableTags};
  }},channels:{async fetch(id, options) {
    reads.push([id,options]);
    if (!channels.has(id)) throw new Error('provider read refused');
    return channels.get(id);
  }}};
  const service=new PrivateRpcService({connectorKey:'fx-tags',transportSecret:'rpc-secret',
    handler:createSdkCapabilityHandler(client,id=>`/channels/${id}`)});
  const port=await service.start();
  const request=async(args)=>{
    const response=await fetch(`http://127.0.0.1:${port}/rpc`,{
      method:'POST',headers:{authorization:'Bearer rpc-secret'},
      body:JSON.stringify({connector_key:'fx-tags',operation:'capabilities',args}),
    });
    return {status:response.status,body:await response.json()};
  };
  return {request,reads,close:()=>service.close()};
}

/** Model the public Guild/ForumChannel accessors rather than raw Discord wire fields. */
function forum({flags=16,availableTags=[{id:'tag-a',moderated:false},
  {id:'tag-mod',moderated:true}],guildId='guild-1'}={}) {
  return {id:'forum-1',guildId,type:15,flags,availableTags,
    isGuild:()=>true,isForum:()=>true,isThread:()=>false,
    isText:()=>false,isAnnouncement:()=>false};
}

test('verified forum flag and live tag identities cross RPC without granting writes',async()=>{
  const h=await harness(new Map([['forum-1',forum()]]));
  try {
    const result=await h.request({channel_id:'forum-1'});
    assert.equal(result.status,200);
    assert.deepEqual(result.body.result,{
      channel_id:'forum-1',guild_id:'guild-1',
      forum_posts:false,message_threads:false,thread_replies:false,
      attachments:false,message_edit_delete:false,webhook_send:false,
      webhook_edit_delete:false,tag_requirements_verified:true,requires_tag:true,
      available_tags:[{id:'tag-a',moderated:false},{id:'tag-mod',moderated:true}],
    });
    assert.deepEqual(h.reads,[['forum-1',{force:true}],['rest','/channels/forum-1']]);
  }finally{await h.close();}
});

test('unknown/missing forum tag metadata cannot be presented as verified',async()=>{
  const h=await harness(new Map([['forum-1',forum({flags:null,
    availableTags:undefined})]]));
  try {
    const response=await h.request({channel_id:'forum-1'});
    assert.equal(response.status,200);
    assert.equal(response.body.result.tag_requirements_verified,false);
    assert.equal(response.body.result.requires_tag,null);
    assert.deepEqual(response.body.result.available_tags,[]);
    assert.equal(response.body.result.forum_posts,false);
  }finally{await h.close();}
});

test('mismatched scope and malformed tag records fail closed at RPC',async()=>{
  const h=await harness(new Map([['forum-1',forum({guildId:null})]]));
  try {
    assert.equal((await h.request({channel_id:'forum-1'})).status,422);
    assert.equal((await h.request({channel_id:'forum-1',forum_id:'other'})).status,422);
    assert.equal(h.reads.length,1);
  }finally{await h.close();}
});

test('fresh REST omission or mismatched forum identity cannot borrow SDK cache tags',async()=>{
  const cached=forum();
  cached.wire={id:'forum-1',guild_id:'guild-1',type:15};
  const h=await harness(new Map([['forum-1',cached]]));
  try {
    const response=await h.request({channel_id:'forum-1'});
    assert.equal(response.status,200);
    assert.equal(response.body.result.tag_requirements_verified,false);
    assert.equal(response.body.result.requires_tag,null);
    cached.wire={id:'forum-1',guild_id:'foreign-guild',type:15,
      flags:16,available_tags:[{id:'tag-a',moderated:false}]};
    const wrong=await h.request({channel_id:'forum-1'});
    assert.equal(wrong.status,422);
  }finally{await h.close();}
});

test('fresh REST omission or mismatched forum identity cannot borrow SDK cache tags',async()=>{
  const cached=forum();
  cached.wire={id:'forum-1',guild_id:'guild-1',type:15};
  const h=await harness(new Map([['forum-1',cached]]));
  try {
    const response=await h.request({channel_id:'forum-1'});
    assert.equal(response.status,200);
    assert.equal(response.body.result.tag_requirements_verified,false);
    assert.equal(response.body.result.requires_tag,null);
    cached.wire={id:'forum-1',guild_id:'foreign-guild',type:15,
      flags:16,available_tags:[{id:'tag-a',moderated:false}]};
    assert.equal((await h.request({channel_id:'forum-1'})).status,422);
  }finally{await h.close();}
});

test('duplicate or invalid tags do not survive as authoritative policy',async()=>{
  for (const tags of [[{id:'dup',moderated:false},{id:'dup',moderated:true}],
    [{id:'bad/tag',moderated:false}], [{id:'tag-no-provenance'}]]) {
    const h=await harness(new Map([['forum-1',forum({availableTags:tags})]]));
    try {
      const result=await h.request({channel_id:'forum-1'});
      assert.equal(result.status,200);
      assert.equal(result.body.result.tag_requirements_verified,false);
      assert.deepEqual(result.body.result.available_tags,[]);
    }finally{await h.close();}
  }
});

test('non-forum or unreadable channel cannot claim forum capability',async()=>{
  const textChannel={...forum(),type:0,isForum:()=>false,isText:()=>true};
  const h=await harness(new Map([['forum-1',textChannel]]));
  try {
    const result=await h.request({channel_id:'forum-1'});
    assert.equal(result.body.result.tag_requirements_verified,false);
    assert.equal(result.body.result.requires_tag,null);
    assert.equal(result.body.result.forum_posts,false);
    assert.deepEqual(result.body.result.available_tags,[]);
    assert.equal((await h.request({channel_id:'unregistered'})).status,503);
  }finally{await h.close();}
});
