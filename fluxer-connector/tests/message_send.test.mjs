/** Physical SDK message and nested reply tests through authenticated Node RPC.
 * The external SDK edge is a stateful fake, not a second production sender.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {PrivateRpcService} from '../src/transport.ts';
import {createSdkMessageSendHandler} from '../src/message_send.ts';

function channel(id, kind='thread', parent='forum-1') {
  return {id,guildId:'guild-1',parentId:kind==='thread'?parent:null,
    isGuild:()=>true,isThread:()=>kind==='thread',
    isText:()=>kind==='text',isAnnouncement:()=>kind==='announcement',
    isForum:()=>kind==='forum',locked:false,archived:false};
}

async function harness({sendThrows=false, denyThread=false, locked=false,
                       mismatchReference=false}={}) {
  const sent=[];
  const messages=new Map([['thread-1/parent-1',{
    id:'parent-1',channelId:'thread-1',guildId:'guild-1',
    author:{id:'user-1',bot:false,username:'Human'},content:'original',
    messageReference:null,webhookId:null,attachments:new Map(),
  }]]);
  const channels=new Map([['forum-1',channel('forum-1','forum')],
    ['thread-1',{...channel('thread-1'),locked}],
    ['thread-2',channel('thread-2')],
    ['text-1',channel('text-1','text')]]);
  const member={id:'bot-1',guild:{id:'guild-1'},roles:{roleIds:['writer']},
    permissionsIn:()=>({has:(flag)=>typeof flag==='bigint'
      ?!denyThread && flag===(1n<<38n)
      : ['ViewChannel','ReadMessageHistory','SendMessages'].includes(flag)})};
  const guild={id:'guild-1',fetchRoles:async()=>[{id:'guild-1'},{id:'writer'}],
    members:{fetch:async()=>member}};
  const client={user:{id:'bot-1',bot:true},guilds:{fetch:async()=>guild},
    channels:{
      async fetch(id,opts){
        assert.equal(opts.force,true);
        return channels.get(id);
      },
      async fetchMessage(container,id){
        return messages.get(`${container}/${id}`);
      },
      async send(container,options){
        sent.push({container,options});
        if(sendThrows)throw Error('remote accepted then connection dropped');
        const id=`result-${sent.length}`;
        const response={id,channelId:container,guildId:'guild-1',
          content:options.content,webhookId:null,attachments:new Map(),
          author:{id:'bot-1',bot:true,username:'Bridge'},
          messageReference:options.replyTo?{channelId:options.replyTo.channelId,
            messageId:mismatchReference?'foreign-id':options.replyTo.messageId,
            guildId:options.replyTo.guildId}:null};
        messages.set(`${container}/${id}`,response);
        return response;
      },
    },
  };
  const rpc=new PrivateRpcService({connectorKey:'fx',transportSecret:'private-secret',
    handler:createSdkMessageSendHandler(client)});
  const port=await rpc.start();
  const request=async(args)=>{
    const res=await fetch(`http://127.0.0.1:${port}/rpc`,{
      method:'POST',headers:{authorization:'Bearer private-secret'},
      body:JSON.stringify({connector_key:'fx',operation:'send_message',args}),
    });
    return {status:res.status,body:await res.json()};
  };
  return {request,sent,messages,channels,close:()=>rpc.close()};
}

const args={container_id:'thread-1',guild_id:'guild-1',reply_to_message_id:'parent-1',
  content:{body:'@everyone safe reply',format:'plain'},
  allowed_mentions:{parse:[]},attempt_key:'create-once-1',delivery_mode:'bot'};

test('one SDK send preserves exact immediate thread parent and suppresses all mentions',async()=>{
  const h=await harness();
  try {
    const answer=await h.request(args);
    assert.equal(answer.status,200);
    assert.deepEqual(answer.body.result,{container_id:'thread-1',message_id:'result-1',
      delivery_mode:'bot'});
    assert.deepEqual(h.sent,[{container:'thread-1',options:{content:'@everyone safe reply',
      allowedMentions:{parse:[],repliedUser:false},
      replyTo:{channelId:'thread-1',messageId:'parent-1',guildId:'guild-1'}}}]);
  }finally{await h.close();}
});

test('non-reply text message keeps container and does not fabricate reply reference',async()=>{
  const h=await harness();
  try {
    const result=await h.request({...args,container_id:'text-1',reply_to_message_id:null});
    assert.equal(result.status,200);
    assert.equal(h.sent[0].container,'text-1');
    assert.ok(!('replyTo' in h.sent[0].options));
    assert.deepEqual(h.sent[0].options.allowedMentions,{parse:[],repliedUser:false});
  }finally{await h.close();}
});

test('unverified same-container reply cannot send a flattened message',async()=>{
  const h=await harness();
  try {
    for (const input of [{...args,reply_to_message_id:'missing'},
      {...args,container_id:'thread-2'},
      {...args,guild_id:'foreign'},
      {...args,reply_to_message_id:'../parent-1'},
      {...args,delivery_mode:'webhook'},
      {...args,allowed_mentions:{parse:['everyone']}},
      {...args,content:{body:'text',format:'markdown'}},
      {...args,unexpected:'new-field'}]) {
      const answer=await h.request(input);
      assert.notEqual(answer.status,200);
    }
    assert.equal(h.sent.length,0);
  }finally{await h.close();}
});

test('missing thread write-bit, locked thread or missing parent all fail before SDK send',async()=>{
  for (const settings of [{denyThread:true},{locked:true}]) {
    const h=await harness(settings);
    try {
      assert.notEqual((await h.request(args)).status,200);
      assert.equal(h.sent.length,0);
    }finally{await h.close();}
  }
  const h=await harness();
  try {
    h.channels.delete('forum-1');
    assert.notEqual((await h.request(args)).status,200);
    assert.equal(h.sent.length,0);
  }finally{await h.close();}
});

test('a lost send response preserves unknown_delivery and never replays inside SDK executor',async()=>{
  const h=await harness({sendThrows:true});
  try {
    const result=await h.request(args);
    assert.equal(result.body.error.reason,'unknown_delivery');
    assert.equal(h.sent.length,1);
  }finally{await h.close();}
});

test('mismatched returned message reference after send is unknown_delivery',async()=>{
  const h=await harness({mismatchReference:true});
  try {
    const response=await h.request(args);
    assert.equal(response.status,422);
    assert.equal(response.body.error.reason,'unknown_delivery');
    assert.equal(h.sent.length,1);
  }finally{await h.close();}
});
