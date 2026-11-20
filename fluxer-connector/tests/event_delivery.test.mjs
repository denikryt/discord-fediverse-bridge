/** Actual Node HTTP -> private Python-style ingress behavior at the wire edge. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {GatewayEventForwarder} from '../src/event_delivery.ts';

const valid = {type: 'MessageDeleted', connector_key:'fx-test',
  message:{connector_key:'fx-test',external_container_id:'thread-1',external_message_id:'m-1'},
  received_at:'2026-10-27T10:00:00.000Z'};
const wait = () => {
  let release;
  return {promise:new Promise(resolve=>{release=resolve;}),release:()=>release()};
};
async function server(handler) {
  const connections = createServer(handler);
  await new Promise(resolve=>connections.listen(0,'127.0.0.1',resolve));
  return {url:`http://127.0.0.1:${connections.address().port}/internal/fluxer/events`,
    close:()=>new Promise(resolve=>connections.close(resolve))};
}
function forwarder(url, params={}) {
  return new GatewayEventForwarder({connectorKey:'fx-test', pythonIngressUrl:url,
    transportSecret:'test-secret-private',...params});
}

test('authenticated normalized event receives an ACK only after sink processed the body', async () => {
  let seen;
  const receiver=await server(async (req,res)=>{
    let raw=''; for await(const part of req) raw+=part;
    seen={url:req.url,auth:req.headers.authorization,
      connector:req.headers['x-fluxer-connector'],dto:JSON.parse(raw)};
    res.writeHead(200,{'content-type':'application/json'});
    res.end(JSON.stringify({accepted:true}));
  });
  const sender=forwarder(receiver.url);
  try {
    await sender.forward(valid);
    assert.deepEqual(seen,{url:'/internal/fluxer/events',auth:'Bearer test-secret-private',
      connector:'fx-test',dto:valid});
  } finally {await sender.close();await receiver.close();}
});

test('wrong connector and oversize DTO refuse before sending HTTP', async () => {
  let calls=0;
  const receiver=await server((_req,res)=>{calls++;res.end('{"accepted":true}');});
  const sender=forwarder(receiver.url);
  try {
    await assert.rejects(sender.forward({...valid,connector_key:'other'}),/connector mismatch/);
    await assert.rejects(sender.forward({...valid,padding:'x'.repeat(300_000)}),/too large/);
    assert.equal(calls,0);
  } finally {await sender.close();await receiver.close();}
});

test('redirect never carries bearer secret to the redirected destination', async () => {
  let leaked=0;
  const target=await server((req,res)=>{if(req.headers.authorization)leaked++;res.end('{}');});
  const source=await server((_req,res)=>{
    res.writeHead(307,{location:target.url});res.end();
  });
  const sender=forwarder(source.url);
  try {
    await assert.rejects(sender.forward(valid),/ingress refused|ACK unknown/);
    assert.equal(leaked,0);
  } finally {await sender.close();await source.close();await target.close();}
});

test('unknown/lost ACK remains unknown and is never automatically resent', async () => {
  let writes=0;
  const receiver=await server(async (_req,res)=>{
    writes++;
    res.writeHead(200,{'content-type':'application/json'});
    res.end('{"accepted":false}');
  });
  const sender=forwarder(receiver.url);
  try {
    await assert.rejects(sender.forward(valid),/ACK unknown/);
    assert.equal(writes,1);
  } finally {await sender.close();await receiver.close();}
});

test('quiesce joins an already-admitted callback, then refuses later callbacks', async () => {
  const entered=wait(),release=wait();
  const receiver=await server(async (_req,res)=>{
    entered.release(); await release.promise;
    res.writeHead(200,{'content-type':'application/json'});
    res.end('{"accepted":true}');
  });
  const sender=forwarder(receiver.url);
  try {
    const ongoing=sender.forward(valid);
    await entered.promise;
    const stopping=sender.close();
    let finished=false; void stopping.then(()=>finished=true);
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(finished,false);
    await assert.rejects(sender.forward(valid),/quiescing/);
    release.release();
    await ongoing;await stopping;
    assert.equal(finished,true);
  } finally {release.release();await sender.close();await receiver.close();}
});

test('disallowed endpoint, query credentials or public HTTP target fail before any request', () => {
  for (const target of ['http://example.test:8888/internal/fluxer/events',
    'http://127.0.0.1:80/other',
    'http://user:pass@127.0.0.1:8888/internal/fluxer/events',
    'http://127.0.0.1:8888/internal/fluxer/events?x=1']) {
    assert.throws(()=>forwarder(target));
  }
});

test('one active ingress slot refuses a second callback without buffering an unbounded queue', async () => {
  const entered=wait(),release=wait();
  let seen=0;
  const receiver=await server(async (_req,res)=>{
    seen++;entered.release();await release.promise;
    res.writeHead(200,{'content-type':'application/json'});
    res.end('{"accepted":true}');
  });
  const sender=forwarder(receiver.url,{maxInFlight:1});
  try {
    const first=sender.forward(valid);
    await entered.promise;
    await assert.rejects(sender.forward(valid),/capacity exhausted/);
    assert.equal(seen,1);
    release.release();await first;
  } finally {release.release();await sender.close();await receiver.close();}
});

test('HTTP 503 is explicitly refused, never an accepted event ACK', async () => {
  const receiver=await server((_req,res)=>{
    res.writeHead(503,{'content-type':'application/json'});
    res.end('{"detail":"quiescing"}');
  });
  const sender=forwarder(receiver.url);
  try {await assert.rejects(sender.forward(valid),/ingress refused/);}
  finally {await sender.close();await receiver.close();}
});

test('unserializable wire DTO is rejected before opening a socket', async () => {
  const receiver=await server((_req,res)=>res.end('{"accepted":true}'));
  const sender=forwarder(receiver.url);
  const cyclic={...valid};cyclic.self=cyclic;
  try {await assert.rejects(sender.forward(cyclic),/invalid Fluxer Gateway wire DTO/);}
  finally {await sender.close();await receiver.close();}
});
