/** Node SDK session → authenticated event HTTP and authenticated RPC lifecycle. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {createServer} from 'node:http';
import {FluxerNodeService} from '../src/service.ts';

async function receiver() {
  const events=[];
  const server=createServer(async(req,res)=>{
    let buf='';for await(const chunk of req) buf+=chunk;
    events.push({body:JSON.parse(buf),auth:req.headers.authorization});
    res.writeHead(200,{'content-type':'application/json'});res.end('{"accepted":true}');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  return {events,port:server.address().port,stop:()=>new Promise(resolve=>server.close(resolve))};
}
function setup(receiverPort,{wrongApi=false,loginError=false,rpcPort}={}) {
  const emitter=new EventEmitter();
  const calls=[];
  const client={on:emitter.on.bind(emitter),off:emitter.off.bind(emitter),
    instance:{endpoints:{api_public:wrongApi?'https://wrong.test':'https://api.example.test/'}},
    rest:{baseUrl:wrongApi?'https://wrong.test/v1':'https://api.example.test/v1'},
    user:undefined,
    async login(token) {
      calls.push(['login',token]);
      if(loginError)throw new Error('secret provider failure');
      this.user={id:'bot-1',bot:true};emitter.emit('ready');
    },
    async destroy(){calls.push(['destroy']);},
  };
  const service=new FluxerNodeService({connectorKey:'fx',origin:'https://example.test',
    apiPublic:'https://api.example.test/',botToken:'bot-secret',
    transportSecret:'private-secret',rpcPort,
    pythonIngressUrl:`http://127.0.0.1:${receiverPort}/internal/fluxer/events`,
    createVettedClient:async()=>client,
    execute:async(operation,args)=>{calls.push([operation,args]);return {id:args.id};},
  });
  return {service,emitter,calls};
}
const request=(port,operation,token='private-secret')=>fetch(`http://127.0.0.1:${port}/rpc`,{
  method:'POST',headers:{authorization:`Bearer ${token}`,'content-type':'application/json'},
  body:JSON.stringify({connector_key:'fx',operation,args:{id:'chan-1'}})});

test('one verified SDK client serves bot RPC while Gateway events reach private HTTP', async()=>{
  const python=await receiver();const {service,emitter,calls}=setup(python.port);
  try {
    const port=await service.start();
    assert.equal((await request(port,'fetch_channel')).status,200);
    emitter.emit('messageDelete',{id:'m1',channelId:'thread-1'});
    await service.close();
    assert.equal(python.events.length,1);
    assert.equal(python.events[0].body.type,'MessageDeleted');
    assert.equal(python.events[0].auth,'Bearer private-secret');
    assert.equal(calls.filter(([name])=>name==='login').length,1);
    assert.equal(calls.filter(([name])=>name==='destroy').length,1);
    assert.equal(emitter.listenerCount('messageDelete'),0);
  } finally {await python.stop();}
});
test('SDK disconnect disables physical RPC without losing health diagnostics',async()=>{
  const python=await receiver();const {service,emitter}=setup(python.port);
  try {
    const port=await service.start();
    emitter.emit('shardDisconnect',0,1006);
    assert.equal((await request(port,'fetch_channel')).status,503);
    const health=await (await request(port,'health')).json();
    assert.equal(health.result.state,'degraded');
    emitter.emit('shardReady',0);
    const after=await (await request(port,'health')).json();
    assert.equal(after.result.gateway_gap,true);
    assert.equal((await request(port,'fetch_channel')).status,200);
    await service.close();
  } finally {await python.stop();}
});
test('endpoint mismatch prevents login, RPC and Gateway while ensuring client teardown',async()=>{
  const python=await receiver();const {service,calls}=setup(python.port,{wrongApi:true});
  try {
    await assert.rejects(service.start(),/initialization refused/);
    assert.deepEqual(calls,[['destroy']]);
    assert.equal(python.events.length,0);
  } finally {await python.stop();}
});

test('RPC port collision after SDK login closes the SDK without claiming a running server',async()=>{
  const python=await receiver();
  const busy=createServer((_req,res)=>res.end());
  await new Promise(resolve=>busy.listen(0,'127.0.0.1',resolve));
  const port=busy.address().port;
  const {service,calls}=setup(python.port,{rpcPort:port});
  try {
    await assert.rejects(service.start(),/initialization refused/);
    assert.equal(calls.filter(([name])=>name==='login').length,1);
    assert.equal(calls.filter(([name])=>name==='destroy').length,1);
  } finally {
    await new Promise(resolve=>busy.close(resolve));
    await python.stop();
  }
});
