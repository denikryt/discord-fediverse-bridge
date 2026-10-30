/** Public Fluxer SDK callback → normalized authenticated HTTP observable flows. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {createServer} from 'node:http';
import {FluxerSdkGatewayHandler} from '../src/gateway.ts';
import {GatewayEventForwarder} from '../src/event_delivery.ts';

const key='fx-test';
const thread={id:'thread-1',guildId:'guild-1',parentId:'forum-1',name:'Topic',type:11};
const message={id:'m1',channelId:'thread-1',guildId:'guild-1',type:0,
  content:'hello',author:{id:'user1',username:'Ada',bot:false},
  attachments:new Map(),createdAt:new Date('2026-10-27T10:00:00Z')};
function pause() {let resolve; const promise=new Promise(r=>{resolve=r;});return {promise,resolve};}
async function listenerServer(onRequest) {
  const server=createServer(async (req,res)=>{
    let body='';for await(const data of req)body+=data;
    await onRequest(req,JSON.parse(body));
    res.writeHead(200,{'content-type':'application/json'});res.end('{"accepted":true}');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  return {port:server.address().port,close:()=>new Promise(resolve=>server.close(resolve))};
}
async function setup(onRequest,maxPending,contextLookupTimeoutMs) {
  const server=await listenerServer(onRequest);
  const events=new EventEmitter();
  // The SDK supplies the same force-fetch manager used by the production path.
  events.channels={async fetchMessage(){throw new Error('unused');},async fetch(id){
    if(id==='thread-1')return {...thread,isThread:()=>true,isGuild:()=>true};
    if(id==='forum-1')return {id:'forum-1',guildId:'guild-1',name:'Forum',
      isForum:()=>true,isGuild:()=>true};
    throw new Error('not found');
  }};
  const failures=[];
  const sink=new GatewayEventForwarder({connectorKey:key,transportSecret:'private-secret',
    pythonIngressUrl:`http://127.0.0.1:${server.port}/internal/fluxer/events`});
  const adapter=new FluxerSdkGatewayHandler({connectorKey:key,forwarder:sink,
    botId:()=> 'bot-id',onProblem:reason=>failures.push(reason),maxPending,contextLookupTimeoutMs});
  adapter.attach(events);
  return {events,failures,adapter,server};
}
test('six SDK event signatures are admitted via actual authenticated HTTP and JSON DTOs',async()=>{
  const requests=[];const s=await setup((req,data)=>{requests.push({auth:req.headers.authorization,data});});
  try {
    s.events.emit('threadCreate',thread);
    s.events.emit('threadUpdate',thread,{...thread,name:'Renamed'});
    s.events.emit('threadDelete',thread);
    s.events.emit('messageCreate',message);
    s.events.emit('messageUpdate',message,{id:'m1',channelId:'thread-1',content:null});
    s.events.emit('messageDelete',{id:'m1',channelId:'thread-1'});
    await s.adapter.close();
    assert.deepEqual(requests.map(r=>r.data.type).sort(),
      ['ThreadCreated','ThreadUpdated','ThreadDeleted','MessageCreated','MessageUpdated','MessageDeleted'].sort());
    assert.ok(requests.every(r=>r.auth==='Bearer private-secret'));
    assert.ok(requests.every(r=>r.data.connector_key===key));
    assert.equal(s.failures.length,0);
    assert.equal(s.events.listenerCount('messageCreate'),0);
  } finally {await s.server.close();}
});
test('shutdown drains an accepted event and refuses later SDK callbacks',async()=>{
  const gate=pause();const entered=pause();const requests=[];
  const s=await setup(async (_req,data)=>{requests.push(data);entered.resolve();await gate.promise;});
  try {
    s.events.emit('messageDelete',{id:'m1',channelId:'thread-1'});
    await entered.promise;
    const closing=s.adapter.close();
    s.events.emit('messageCreate',message);
    assert.equal(requests.length,1);
    gate.resolve();await closing;
    assert.equal(s.events.listenerCount('messageDelete'),0);
  } finally {gate.resolve();await s.server.close();}
});
test('incomplete SDK observations and bounded overload cannot publish or be silently ACKed',async()=>{
  const gate=pause();const entered=pause();const requests=[];
  const s=await setup(async (_req,data)=>{requests.push(data);entered.resolve();await gate.promise;},1);
  try {
    s.events.emit('threadCreate',{...thread,parentId:null});
    s.events.emit('messageDelete',{id:'m1',channelId:'thread-1'});
    await entered.promise;
    s.events.emit('messageDelete',{id:'m2',channelId:'thread-1'});
    assert.deepEqual(s.failures.sort(),['admission_failed','normalization_failed'].sort());
    gate.resolve();await s.adapter.close();
    assert.equal(requests.length,1);
  } finally {gate.resolve();await s.server.close();}
});

test('uncached forum message is enriched only after forced SDK thread and parent reads',async()=>{
  const requests=[];const reads=[];
  const s=await setup((_req,data)=>{requests.push(data);});
  s.events.channels={async fetchMessage(){throw new Error('unused');},async fetch(id,options){
    reads.push([id,options]);
    if(id==='thread-1')return {...thread,isThread:()=>true,isGuild:()=>true,
      isForum:()=>false,isText:()=>false,isAnnouncement:()=>false};
    if(id==='forum-1')return {id:'forum-1',guildId:'guild-1',name:'Forum',
      isThread:()=>false,isGuild:()=>true,isForum:()=>true,
      isText:()=>false,isAnnouncement:()=>false};
    throw new Error('missing');
  }};
  // Listener admission should use this source's channel manager rather than raw payload.
  try {
    s.events.emit('messageCreate',message);
    await s.adapter.close();
    assert.deepEqual(reads,[['thread-1',{force:true}],['forum-1',{force:true}]]);
    assert.equal(requests.length,1);
    assert.deepEqual(requests[0].thread_context,{channel:{connector_key:key,
      external_channel_id:'forum-1',kind:'forum',external_guild_id:'guild-1'},
      external_thread_id:'thread-1'});
    assert.deepEqual(requests[0].parent_channel,requests[0].thread_context.channel);
    assert.deepEqual(s.failures,[]);
  }finally{await s.server.close();}
});

test('mismatched parent or guild forwards only an unverified, nonclaiming message observation',async()=>{
  const requests=[];
  const s=await setup((_req,data)=>{requests.push(data);});
  s.events.channels={async fetchMessage(){throw new Error('unused');},async fetch(id){
    if(id==='thread-1')return {...thread,guildId:'other-guild',isThread:()=>true,
      isGuild:()=>true,isForum:()=>false,isText:()=>false,isAnnouncement:()=>false};
    return {id:'forum-1',guildId:'other-guild',isForum:()=>true,isGuild:()=>true,
      isThread:()=>false,isText:()=>false,isAnnouncement:()=>false};
  }};
  try{
    s.events.emit('messageCreate',message);
    await s.adapter.close();
    assert.equal(requests.length,1);
    assert.equal('thread_context' in requests[0],false);
    assert.equal('parent_channel' in requests[0],false);
    assert.deepEqual(s.failures,['context_unverified']);
  }finally{await s.server.close();}
});

test('shutdown joins a blocked parent lookup and HTTP forward, refusing later events',async()=>{
  const readGate=pause();const started=pause();const requests=[];
  const s=await setup((_req,data)=>{requests.push(data);});
  s.events.channels={async fetchMessage(){throw new Error('unused');},async fetch(id){
    if(id==='thread-1'){
      started.resolve();await readGate.promise;
      return {...thread,isThread:()=>true,isGuild:()=>true};
    }
    return {id:'forum-1',guildId:'guild-1',isForum:()=>true,isGuild:()=>true};
  }};
  try {
    s.events.emit('messageCreate',message);
    await started.promise;
    const closing=s.adapter.close();
    s.events.emit('messageCreate',{...message,id:'late'});
    assert.equal(requests.length,0);
    let completed=false;
    void closing.then(()=>{completed=true;});
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(completed,false);
    readGate.resolve();await closing;
    assert.equal(requests.length,1);
  }finally{readGate.resolve();await s.server.close();}
});

test('expired metadata deadline forwards unclaimed observation but close joins the original SDK read',async()=>{
  const blocked=pause();const entered=pause();const requests=[];
  const s=await setup((_req,data)=>{requests.push(data);entered.resolve();},1,15);
  s.events.channels={async fetchMessage(){throw new Error('unused');},async fetch(id){
    if(id==='thread-1'){
      await blocked.promise;
      return {...thread,isThread:()=>true,isGuild:()=>true};
    }
    return {id:'forum-1',guildId:'guild-1',isGuild:()=>true,isForum:()=>true};
  }};
  // Configure a short metadata deadline without imposing a timeout on the
  // underlying SDK socket: close must still join the in-flight provider read.
  try {
    s.events.emit('messageCreate',message);
    await entered.promise;
    assert.equal('thread_context' in requests[0],false);
    assert.deepEqual(s.failures,['context_unverified']);
    const closing=s.adapter.close();
    let stopped=false;
    void closing.then(()=>{stopped=true;});
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(stopped,false);
    s.events.emit('messageCreate',{...message,id:'late'});
    blocked.resolve();
    await closing;
    assert.equal(requests.length,1);
  }finally{blocked.resolve();await s.adapter.close();await s.server.close();}
});
