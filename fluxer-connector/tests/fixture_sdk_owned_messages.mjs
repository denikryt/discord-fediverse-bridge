/** SDK-session-backed Node fixture: real RPC wire with stateful remote message effects.
 *
 * Only the external Fluxer SDK and REST write provider are faked. The session
 * READY/health, authenticated RPC and Python adapter boundaries remain real.
 */
import {EventEmitter} from 'node:events';
import {FluxerNodeService} from '../src/service.ts';
import {createSdkReadHandler} from '../src/rest_reads.ts';
import {PrivateRpcError} from '../src/transport.ts';

/** Materialize the SDK's observed channel shape for this test build. */
function channel(id, kind, parentId=null) {
  return {id, guildId:'guild-1', parentId, name:id,
    isGuild:()=>true, isThread:()=>kind==='thread',isForum:()=>kind==='forum',
    isText:()=>kind==='text',isAnnouncement:()=>false};
}

const emitter = new EventEmitter();
const messages = new Map([
  ['thread-1/owned',{id:'owned',channelId:'thread-1',guildId:'guild-1',
    content:'original',author:{id:'bot-one',bot:true,username:'Bridge'},
    webhookId:null,createdAt:new Date('2026-10-28T12:00:00Z'),editedAt:null,attachments:new Map()}],
  ['thread-1/foreign',{id:'foreign',channelId:'thread-1',guildId:'guild-1',
    content:'other bot',author:{id:'bot-two',bot:true,username:'Other'},
    webhookId:null,createdAt:new Date('2026-10-28T12:00:00Z'),editedAt:null,attachments:new Map()}],
  ['thread-1/webhook',{id:'webhook',channelId:'thread-1',guildId:'guild-1',
    content:'webhook',author:{id:'bot-one',bot:true,username:'WebHook'},
    webhookId:'hook-1',createdAt:new Date('2026-10-28T12:00:00Z'),editedAt:null,attachments:new Map()}],
]);
const client={
  on:emitter.on.bind(emitter),off:emitter.off.bind(emitter),
  instance:{endpoints:{api_public:'https://api.fx.test/'}},
  rest:{baseUrl:'https://api.fx.test/v1'},user:null,
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
      return item;
    },
  },
  async login(_token){this.user={id:'bot-one',bot:true};emitter.emit('ready');},
  async destroy(){},
};
const read=createSdkReadHandler(client);
/** Route observed writes to a single stateful fake provider, not guessed SDK REST routes. */
async function execute(operation,args) {
  if(operation==='fetch_channel'||operation==='fetch_thread'||operation==='fetch_message') {
    return read(operation,args);
  }
  if(operation==='capabilities')return {channel_id:args.channel_id,guild_id:'guild-1',
    message_edit_delete:true};
  if(operation==='check_permissions')return {bot_id:client.user.id,
    channel_id:args.channel_id,guild_id:'guild-1',
    ...Object.fromEntries(args.actions.map(action=>[action,'allowed']))};
  if(operation==='edit_message'||operation==='delete_message'){
    if(args.bot_id!==client.user.id)throw new PrivateRpcError('forbidden',403);
    const key=`${args.container_id}/${args.message_id}`;
    const msg=messages.get(key);
    if(!msg)throw new PrivateRpcError('not_found',404);
    if(msg.author.id!==client.user.id||msg.webhookId!==null){
      throw new PrivateRpcError('forbidden',403);
    }
    if(operation==='edit_message')msg.content=args.content.body;
    else messages.delete(key);
    return {container_id:args.container_id,message_id:args.message_id};
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
