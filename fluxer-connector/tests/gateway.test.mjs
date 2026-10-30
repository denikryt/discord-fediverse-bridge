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
async function setup(onRequest,maxPending) {
  const server=await listenerServer(onRequest);
  const events=new EventEmitter();
  const failures=[];
  const sink=new GatewayEventForwarder({connectorKey:key,transportSecret:'private-secret',
    pythonIngressUrl:`http://127.0.0.1:${server.port}/internal/fluxer/events`});
  const adapter=new FluxerSdkGatewayHandler({connectorKey:key,forwarder:sink,
    botId:()=> 'bot-id',onProblem:reason=>failures.push(reason),maxPending});
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
