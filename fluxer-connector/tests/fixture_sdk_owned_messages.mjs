/** SDK-session-backed Node fixture: real RPC wire with stateful remote message effects.
 *
 * Only the external Fluxer SDK and REST write provider are faked. The session
 * READY/health, authenticated RPC and Python adapter boundaries remain real.
 */
import {EventEmitter} from 'node:events';
import {FluxerNodeService} from '../src/service.ts';
import {createSdkReadHandler} from '../src/rest_reads.ts';
import {createSdkMessageMutationHandler} from '../src/message_mutations.ts';
import {PrivateRpcError} from '../src/transport.ts';

/** Materialize the SDK's observed channel shape for this test build. */
function channel(id, kind, parentId=null) {
  return {id, guildId:'guild-1', parentId, name:id,
    isGuild:()=>true, isThread:()=>kind==='thread',isForum:()=>kind==='forum',
    isText:()=>kind==='text',isAnnouncement:()=>false};
}

const emitter = new EventEmitter();
const messages = new Map([
  // The measured SDK GET has null guildId. Only the fresh channel supplies
  // guild scope; webhook provenance and bot ownership remain explicit.
  ['thread-1/owned',{id:'owned',channelId:'thread-1',guildId:null,
    content:'original',author:{id:'bot-one',bot:true,username:'Bridge'},
    webhookId:null,createdAt:new Date('2026-10-28T12:00:00Z'),editedAt:null,attachments:new Map()}],
  ['thread-1/foreign',{id:'foreign',channelId:'thread-1',guildId:null,
    content:'other bot',author:{id:'bot-two',bot:true,username:'Other'},
    webhookId:null,createdAt:new Date('2026-10-28T12:00:00Z'),editedAt:null,attachments:new Map()}],
  ['thread-1/webhook',{id:'webhook',channelId:'thread-1',guildId:null,
    content:'webhook',author:{id:'bot-one',bot:true,username:'WebHook'},
    webhookId:'hook-1',createdAt:new Date('2026-10-28T12:00:00Z'),editedAt:null,attachments:new Map()}],
]);
const client={
  on:emitter.on.bind(emitter),off:emitter.off.bind(emitter),
  instance:{endpoints:{api_public:'https://api.fx.test/'}},
  rest:{baseUrl:'https://api.fx.test/v1'},user:null,
  guilds:{async fetch(id,options){
    if(id!=='guild-1'||options?.force!==true)throw new PrivateRpcError('not_found',404);
    return {id,fetchRoles:async()=>[{id},{id:'writer'}],members:{
      fetch:async(botId,params)=>({id:botId,guild:{id},roles:{roleIds:['writer']},
        permissionsIn:()=>({has:flag=>['ViewChannel','ReadMessageHistory'].includes(flag)})}),
    }};
  }},
  channels:{
    async fetch(id,options){
      if(options?.force!==true)throw new Error('read must bypass cache');
      if(id==='thread-1')return channel(id,'thread','forum-1');
      if(id==='forum-1')return channel(id,'forum');
      throw new PrivateRpcError('not_found',404);
    },
    async fetchMessage(container,id){
      const item=messages.get(`${container}/${id}`);
      if(!item)throw new PrivateRpcError('not_found',404);
      // SDK Message.fetch is a fresh REST GET and returns a Message with
      // edit()/delete() methods, both mutating this stateful fake remote.
      return {...item,
        async edit(payload){
          if(item.author.id!=='bot-one'||item.webhookId!==null){
            throw new PrivateRpcError('forbidden',403);
          }
          item.content=payload.content;
          item.editedAt=new Date('2026-10-28T12:01:00Z');
          return {...item};
        },
        async delete(){
          if(item.author.id!=='bot-one'||item.webhookId!==null){
            throw new PrivateRpcError('forbidden',403);
          }
          messages.delete(`${container}/${id}`);
        },
      };
    },
  },
  async login(_token){this.user={id:'bot-one',bot:true};emitter.emit('ready');},
  async destroy(){},
};
const read=createSdkReadHandler(client);
const mutate=createSdkMessageMutationHandler(client);
/** The official SDK fake edge owns only its remote state, not another RPC sender. */
async function execute(operation,args) {
  if(operation==='fetch_channel'||operation==='fetch_thread'||operation==='fetch_message') {
    return read(operation,args);
  }
  if(operation==='capabilities')return {channel_id:args.channel_id,guild_id:'guild-1',
    message_edit_delete:true};
  if(operation==='check_permissions')return {bot_id:client.user.id,
    channel_id:args.channel_id,guild_id:'guild-1',
    ...Object.fromEntries(args.actions.map(action=>[action,'allowed']))};
  if(operation==='edit_message'||operation==='delete_message') {
    return mutate(operation,args);
  }
  throw new PrivateRpcError('unsupported');
}

const service=new FluxerNodeService({connectorKey:'fx-owned',
  origin:'https://fx.test',apiPublic:'https://api.fx.test/',botToken:'node-only-token',
  transportSecret:'test-transport-secret',
  pythonIngressUrl:'http://127.0.0.1:43199/internal/fluxer/events',
  createVettedClient:async()=>client,execute});
const port=await service.start();
process.stdout.write(`${port}\n`);
process.on('SIGTERM',()=>{void service.close().then(()=>process.exit(0));});
