import test from 'node:test';
import assert from 'node:assert/strict';
import {PrivateRpcService} from '../src/transport.ts';

function fixture(handler = async (_, args) => ({id: args.id})) {
  const service = new PrivateRpcService({connectorKey: 'fluxer-test',
    transportSecret: 'test-only-secret', handler});
  return service;
}
function post(port, data, token = 'test-only-secret') {
  return fetch(`http://127.0.0.1:${port}/rpc`, {method: 'POST',
    headers: {'authorization': `Bearer ${token}`, 'content-type': 'application/json'},
    body: JSON.stringify(data)});
}
const valid = {connector_key: 'fluxer-test', operation: 'fetch_channel', args: {id: 'opaque'}};

test('RPC refuses unsafe bearer even when instantiated without main.ts', () => {
  for (const secret of ['1234567', '1234567é', 'секретсекрет',
    'abc\rdefgh', 'abc\ndefgh', 'abc\tdefgh', 'abc defgh', 'abcdefgh\x7f']) {
    assert.throws(() => new PrivateRpcService({connectorKey: 'fluxer-test',
      transportSecret: secret, handler: async () => ({})}),
      /invalid Fluxer private RPC configuration/);
  }
  assert.doesNotThrow(() => new PrivateRpcService({connectorKey: 'fluxer-test',
    transportSecret: 'aB1-._~+/=', handler: async () => ({})}));
});

test('authenticated read routes exact operation and returns scoped success', async () => {
  const observed = [];
  const server = fixture(async (op, args) => {observed.push([op, args]); return {id: args.id};});
  const port = await server.start();
  try {
    const response = await post(port, valid);
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), {connector_key: 'fluxer-test', ok: true,
      result: {id: 'opaque'}});
    assert.deepEqual(observed, [['fetch_channel', {id: 'opaque'}]]);
  } finally {await server.close();}
});

test('authorization, method and connector checks never enter provider', async () => {
  let invoked = 0;
  const server = fixture(async () => {invoked++; return {};});
  const port = await server.start();
  try {
    assert.equal((await post(port, valid, 'bad')).status, 403);
    assert.equal((await post(port, {...valid, operation: 'unknown_rpc'})).status, 422);
    assert.equal((await post(port, {...valid, connector_key: 'foreign'})).status, 422);
    assert.equal((await fetch(`http://127.0.0.1:${port}/rpc`)).status, 404);
    assert.equal(invoked, 0);
  } finally {await server.close();}
});

test('oversized and malformed bodies fail without provider effects', async () => {
  let invoked = 0;
  const server = fixture(async () => {invoked++; return {};});
  const port = await server.start();
  try {
    const response = await post(port, {...valid, args: {id: 'x'.repeat(300_000)}});
    assert.equal(response.status, 413);
    const malformed = await fetch(`http://127.0.0.1:${port}/rpc`, {method:'POST',
      headers: {authorization: 'Bearer test-only-secret'}, body: 'invalid json'});
    assert.equal(malformed.status, 422);
    assert.equal(invoked, 0);
  } finally {await server.close();}
});

test('shutdown joins accepted provider operation', async () => {
  let release;
  let entered;
  const admitted = new Promise(resolve => entered = resolve);
  const gate = new Promise(resolve => release = resolve);
  const server = fixture(async () => { entered(); await gate; return {done: true}; });
  const port = await server.start();
  const request = post(port, valid);
  await admitted;
  const stop = server.close();
  let complete = false;
  void stop.then(() => {complete = true;});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(complete, false);
  release();
  assert.equal((await request).status, 200);
  await stop;
  assert.equal(complete, true);
});
