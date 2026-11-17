/** Cross-process forum-create fixture with a real Node SDK session/RPC owner.
 * The fake SDK's REST boundary persists the observed remote post in memory.
 */
import {EventEmitter} from 'node:events';
import {appendFileSync, readFileSync} from 'node:fs';
import {FluxerNodeService} from '../src/service.ts';
import {createProductionRpcHandler} from '../src/main.ts';
import {PrivateRpcError} from '../src/transport.ts';

const logPath=process.argv[2];
const aclPath=process.argv[5];
// Tests can revoke only the parent or thread privilege after creating a post.
const currentAcl=()=>aclPath ? JSON.parse(readFileSync(aclPath, 'utf8')) :
  {forum_send:true, thread_send:true};
if (!logPath) throw Error('test requires external effects log');
// Exercise both explicitly populated snapshots and the measured full REST
// omission shape, including a starter whose observed ID equals its thread ID.
const restOmissions=process.argv[4]==='rest-omissions';
const starterId=restOmissions?'thread-9':'starter-91';
const emitter=new EventEmitter();
const threads=new Map();
const messages=new Map();
function channel(id, kind, parentId=null) {
  return {id, name:id, guildId:'guild-1', parentId,
    type:kind==='forum'?15:11,isGuild:()=>true,
    isForum:()=>kind==='forum',isThread:()=>kind==='thread',
    isText:()=>false,isAnnouncement:()=>false,archived:false,locked:false};
}
const guild={id:'guild-1',fetchRoles:async()=>[{id:'guild-1'},{id:'writer'}],
  members:{async fetch(id){return {id,guild:{id:'guild-1'},roles:{roleIds:['writer']},
    permissionsIn:(scope)=>({has:(name)=>{
      const acl=currentAcl();
      if(typeof name==='bigint') return name===(1n<<38n) && acl.thread_send;
      if(name==='SendMessages') return scope.isThread() || acl.forum_send;
      return ['ViewChannel','ReadMessageHistory'].includes(name);
    }})};}}};
const client={on:emitter.on.bind(emitter),off:emitter.off.bind(emitter),
  instance:{endpoints:{api_public:'https://api.fx.test/'}},
  user:null,guilds:{fetch:async()=>guild},
  channels:{
    async fetch(id,opts){
      if(opts?.force!==true)throw Error('not a fresh read');
      if(id==='forum-1')return channel(id,'forum');
      if(threads.has(id))return channel(id,'thread','forum-1');
      throw new PrivateRpcError('not_found',404);
    },
    async fetchMessage(container,id){
      const msg=messages.get(`${container}/${id}`);
      if(!msg)throw new PrivateRpcError('not_found',404);
      return msg;
    },
    async send(container,options){
      appendFileSync(logPath, `${JSON.stringify({sdk_send:{container,options}})}\n`);
      if(process.argv[3]==='lose-message-response')throw Error('message ACK lost');
      const msg={id:'message-12',channelId:container,guildId:restOmissions?null:'guild-1',
        content:options.content,author:{id:'bot-1',bot:true,username:'Bridge'},
        webhookId:null,createdAt:new Date('2026-10-28T12:00:00Z'),editedAt:null,
        attachments:new Map(),messageReference:options.replyTo?{
          channelId:options.replyTo.channelId,messageId:options.replyTo.messageId,
          guildId:options.replyTo.guildId}:null};
      messages.set(`${container}/message-12`,msg);
      return msg;
    },
  },
  rest:{baseUrl:'https://api.fx.test/v1',
    async get(route){
      if(route==='/channels/forum-1')return {id:'forum-1',guild_id:'guild-1',
        type:15,flags:16,available_tags:[{id:'tag-a',moderated:false}]};
      throw Error('invalid REST path');
    },
    async post(route,opts){
      if(route!=='/channels/forum-1/threads')throw Error('invalid REST write');
      appendFileSync(logPath, `${JSON.stringify({route,options:opts})}\n`);
      threads.set('thread-9',opts.body);
      const msg={id:starterId,channelId:'thread-9',guildId:restOmissions?null:'guild-1',
        content:opts.body.message.content,
        author:{id:'bot-1',bot:true,username:'Bridge'},webhookId:null,
        createdAt:new Date('2026-10-28T12:00:00Z'),editedAt:null,attachments:new Map()};
      messages.set(`thread-9/${starterId}`,msg);
      if(process.argv[3]==='lose-response')throw Error('external response lost');
      return {id:'thread-9',parent_id:'forum-1',guild_id:'guild-1',type:11,
        message:{id:starterId,channel_id:'thread-9',
          ...(restOmissions?{}:{guild_id:'guild-1',webhook_id:null}),
          author:{id:'bot-1',bot:true},content:opts.body.message.content}};
    },
  },
  async login(){this.user={id:'bot-1',bot:true};emitter.emit('ready');},
  async destroy(){},
};
// Exercise the same dispatcher the production sidecar actually composes.
const execute=createProductionRpcHandler(client, {
  channel:id=>`/channels/${id}`,
  channelThreads:id=>`/channels/${id}/threads`,
}, {writeContractVerified:true});
const service=new FluxerNodeService({connectorKey:'fx-forum',
  origin:process.env.FLUXER_TEST_ORIGIN ?? 'https://fx.test',
  apiPublic:'https://api.fx.test/',botToken:'node-only-secret',
  transportSecret:'shared-ipc-secret',
  pythonIngressUrl:'http://127.0.0.1:43299/internal/fluxer/events',
  createVettedClient:async()=>client,
  execute,
});
const port=await service.start();
process.stdout.write(`${port}\n`);
process.on('SIGTERM',()=>{void service.close().then(()=>process.exit(0));});
