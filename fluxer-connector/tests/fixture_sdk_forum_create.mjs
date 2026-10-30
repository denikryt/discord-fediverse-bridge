/** Cross-process forum-create fixture with a real Node SDK session/RPC owner.
 * The fake SDK's REST boundary persists the observed remote post in memory.
 */
import {EventEmitter} from 'node:events';
import {appendFileSync} from 'node:fs';
import {FluxerNodeService} from '../src/service.ts';
import {createSdkForumCreateHandler} from '../src/forum_create.ts';
import {createSdkMessageSendHandler} from '../src/message_send.ts';
import {createSdkReadHandler} from '../src/rest_reads.ts';
import {PrivateRpcError} from '../src/transport.ts';

const logPath=process.argv[2];
if (!logPath) throw Error('test requires external effects log');
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
    permissionsIn:()=>({has:(name)=>typeof name==='bigint' ? name===(1n<<38n)
      :['ViewChannel','ReadMessageHistory','SendMessages'].includes(name)})};}}};
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
      const msg={id:'message-12',channelId:container,guildId:'guild-1',
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
      const msg={id:'starter-91',channelId:'thread-9',guildId:'guild-1',
        content:opts.body.message.content,
        author:{id:'bot-1',bot:true,username:'Bridge'},webhookId:null,
        createdAt:new Date('2026-10-28T12:00:00Z'),editedAt:null,attachments:new Map()};
      messages.set('thread-9/starter-91',msg);
      if(process.argv[3]==='lose-response')throw Error('external response lost');
      return {id:'thread-9',parent_id:'forum-1',guild_id:'guild-1',type:11,
        message:{id:'starter-91',channel_id:'thread-9',guild_id:'guild-1',
          author:{id:'bot-1',bot:true},webhook_id:null}};
    },
  },
  async login(){this.user={id:'bot-1',bot:true};emitter.emit('ready');},
  async destroy(){},
};
const reads=createSdkReadHandler(client);
const create=createSdkForumCreateHandler(client,id=>`/channels/${id}`,
  id=>`/channels/${id}/threads`);
const send=createSdkMessageSendHandler(client);
const service=new FluxerNodeService({connectorKey:'fx-forum',origin:'https://fx.test',
  apiPublic:'https://api.fx.test/',botToken:'node-only-secret',
  transportSecret:'shared-ipc-secret',
  pythonIngressUrl:'http://127.0.0.1:43299/internal/fluxer/events',
  createVettedClient:async()=>client,
  execute:async(operation,args)=>{
    if(operation==='fetch_channel'||operation==='fetch_thread'||operation==='fetch_message'){
      return reads(operation,args);
    }
    if(operation==='capabilities')return {channel_id:args.channel_id,guild_id:'guild-1',
      forum_posts:true,thread_replies:true,tag_requirements_verified:true,requires_tag:true,
      available_tags:[{id:'tag-a',moderated:false}]};
    if(operation==='check_permissions')return {bot_id:client.user.id,
      channel_id:args.channel_id,guild_id:'guild-1',
      ...Object.fromEntries(args.actions.map(action=>[action,'allowed']))};
    if(operation==='create_forum_thread')return create(operation,args);
    if(operation==='send_message')return send(operation,args);
    throw new PrivateRpcError('unsupported');
  },
});
const port=await service.start();
process.stdout.write(`${port}\n`);
process.on('SIGTERM',()=>{void service.close().then(()=>process.exit(0));});
