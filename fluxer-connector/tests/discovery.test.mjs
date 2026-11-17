import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { discoverInstance, validateOrigin, validateDiscoveryDocument, validateGatewayConnection } from '../src/discovery.ts';

test('production discovery refuses userinfo, fragments, path, HTTP and metadata origin', () => {
  for (const origin of ['http://example.test', 'https://user:pass@example.test',
    'https://example.test/', 'https://example.test?x=1',
    'https://example.test#fragment', 'https://169.254.169.254/anything']) {
    assert.throws(() => validateOrigin(origin));
  }
});

test('dotted DNS origins cannot be trusted as a different authenticated identity', () => {
  assert.throws(() => validateOrigin('https://fx.test.'));
  assert.equal(validateOrigin('https://FX.test:443').origin, 'https://fx.test');
  assert.equal(validateOrigin('https://other.test').origin, 'https://other.test');
});

test('Unicode IDN requires explicit ASCII A-label on every self-hosted identity', () => {
  for (const origin of ['https://faß.test', 'https://bücher.test']) {
    assert.throws(() => validateOrigin(origin));
  }
  assert.equal(validateOrigin('https://XN--FA-HIA.test:443').origin,
    'https://xn--fa-hia.test');
  assert.notEqual(validateOrigin('https://xn--fa-hia.test').origin,
    validateOrigin('https://fass.test').origin);
});

test('bot REST needs api_public and an approved self-hosted API host', () => {
  assert.throws(() => validateDiscoveryDocument('https://example.test',
    {endpoints: {api: 'https://example.test'}}));
  assert.throws(() => validateDiscoveryDocument('https://example.test',
    {endpoints: {api_public: 'https://outside.test'}}));
  const doc = validateDiscoveryDocument('https://example.test',
    {endpoints: {api_public: 'https://api.example.test',
      gateway: 'wss://gateway.example.test', api: 'https://web.example.test'}},
    {allowedEndpointHosts: ['api.example.test', 'gateway.example.test']});
  assert.equal(doc.apiPublic, 'https://api.example.test/');
  assert.equal(doc.gateway, 'wss://gateway.example.test/');
});

test('loopback development discovery performs an actual bounded GET without token', async () => {
  const seen = [];
  const server = createServer((req, res) => {
    seen.push([req.url, req.headers.authorization]);
    res.setHeader('content-type', 'application/json');
    res.end(JSON.stringify({endpoints: {api_public: `http://127.0.0.1:${server.address().port}`,
      gateway: `ws://127.0.0.1:${server.address().port}`}}));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  try {
    const result = await discoverInstance(`http://127.0.0.1:${port}`, {
      allowInsecureLocalDevelopment: true,
      resolver: async () => [{address: '127.0.0.1', family: 4}],
    });
    assert.equal(result.apiPublic, `http://127.0.0.1:${port}/`);
    assert.deepEqual(seen, [['/.well-known/fluxer', undefined]]);
  } finally {await new Promise(resolve => server.close(resolve));}
});

test('private DNS answers rejected before network side effects', async () => {
  for (const address of ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fc00::1']) {
    await assert.rejects(() => discoverInstance('https://example.test',
      {resolver: async () => [{address, family: address.includes(':') ? 6 : 4}]}));
  }
});

test('redirects never followed to other API hosts', async () => {
  let received = 0;
  const server = createServer((_req, res) => {
    received += 1;
    res.writeHead(302, {Location: 'http://169.254.169.254/latest'});
    res.end();
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    await assert.rejects(() => discoverInstance(`http://127.0.0.1:${server.address().port}`, {
      allowInsecureLocalDevelopment: true,
      resolver: async () => [{address: '127.0.0.1', family: 4}],
    }));
    assert.equal(received, 1);
  } finally {await new Promise(resolve => server.close(resolve));}
});

test('unapproved or missing Gateway endpoint is rejected before any SDK login', () => {
  for (const gateway of [undefined, 'ws://gateway.example.test',
    'wss://other.example.test', 'wss://user:token@gateway.example.test']) {
    const endpoints = {api_public: 'https://api.example.test'};
    if (gateway !== undefined) endpoints.gateway = gateway;
    assert.throws(() => validateDiscoveryDocument('https://example.test', {endpoints},
      {allowedEndpointHosts: ['api.example.test','gateway.example.test']}));
  }
});

test('SDK Gateway host admission checks target URL on every socket construction', () => {
  const trusted = 'wss://gateway.example.test/';
  assert.doesNotThrow(() => validateGatewayConnection(
    'wss://gateway.example.test/?v=1&encoding=json', trusted));
  for (const candidate of ['ws://gateway.example.test/?v=1&encoding=json',
    'wss://attacker.test/?v=1&encoding=json',
    'wss://bot:token@gateway.example.test/?v=1&encoding=json',
    'wss://gateway.example.test/?v=1&encoding=json&redirect=evil']) {
    assert.throws(() => validateGatewayConnection(candidate, trusted));
  }
});
