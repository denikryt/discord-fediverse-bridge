/** Local SDK-boundary fake for Python→Node→FluxerAdapter read scenarios. */
import {PrivateRpcService} from '../src/transport.ts';
import {createSdkReadHandler} from '../src/rest_reads.ts';

/** Minimal observed SDK channel shape for a cross-process lookup fixture. */
function channel(id, kind, parentId=null) {
  return {id, guildId:'guild-1', parentId, name:id,
    isForum:()=>kind==='forum', isGuild:()=>true, isThread:()=>kind==='thread',
    isText:()=>kind==='text', isAnnouncement:()=>kind==='announcement'};
}

const channels=new Map([
  ['forum-1',channel('forum-1','forum')],
  ['thread-1',channel('thread-1','thread','forum-1')],
]);
const messages=new Map([['thread-1/message-1',{
  id:'message-1',channelId:'thread-1',guildId:'guild-1',content:'hello from SDK',
  author:{id:'user-1',username:'reader',bot:false},webhookId:null,
  createdAt:new Date('2026-10-28T00:00:00Z'),editedAt:null,
  attachments:new Map(),
}]]);
const client={channels:{
  async fetch(id) {
    if(!channels.has(id)) throw new Error('not found');
    return channels.get(id);
  },
  async fetchMessage(container,id) {
    if(!messages.has(`${container}/${id}`))throw new Error('not found');
    return messages.get(`${container}/${id}`);
  },
}};
const service=new PrivateRpcService({connectorKey:'fluxer-contract',
  transportSecret:'test-transport-secret',handler:createSdkReadHandler(client)});
const port=await service.start();
process.stdout.write(`${port}\n`);
process.on('SIGTERM',()=>{void service.close().then(()=>process.exit(0));});
