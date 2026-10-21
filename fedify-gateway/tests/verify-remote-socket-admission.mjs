/** Scenario coverage of destination admission at the actual HTTPS dial boundary. */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createServer } from 'node:http';
import { registerHooks } from 'node:module';

// Runtime has no npm dependencies: map the project's emitted .js imports back
// to TypeScript sources for these Node built-in-only boundary checks.
registerHooks({
  resolve(specifier, context, next) {
    if (specifier.startsWith('.') && specifier.endsWith('.js') && context.parentURL?.includes('/fedify-gateway/src/')) {
      return next(specifier.replace(/\.js$/, '.ts'), context);
    }
    return next(specifier, context);
  },
});
const {
  validateResolvedRemoteAddresses,
  admitRemoteDeliveryEndpoint,
} = await import('../src/remote-delivery-endpoints.ts');
const { sendSignedJsonActivity } = await import('../src/outbound/signed-json-delivery.ts');

test('public A and AAAA records may be used for a physical dial', () => {
  assert.equal(validateResolvedRemoteAddresses([{address: '8.8.8.8', family: 4}]).address, '8.8.8.8');
  assert.equal(validateResolvedRemoteAddresses([{address: '2606:4700:4700::1111', family: 6}]).family, 6);
  assert.equal(admitRemoteDeliveryEndpoint('https://relay.example/inbox'), 'https://relay.example/inbox');
});

test('private or special-purpose resolved address blocks the entire DNS answer', () => {
  for (const address of [
    '127.0.0.1', '10.0.0.1', '169.254.169.254', '172.31.0.1',
    '192.168.0.2', '100.100.100.100', '198.18.1.2', '192.0.2.1',
    '::1', 'fe80::1', 'fc00::1', '2001:db8::1', '::ffff:127.0.0.1',
  ]) {
    assert.throws(() => validateResolvedRemoteAddresses([{address, family: address.includes(':') ? 6 : 4}]), /public|private|unsafe/i, address);
  }
  assert.throws(() => validateResolvedRemoteAddresses([
    {address:'8.8.8.8',family:4}, {address:'127.0.0.1',family:4},
  ]), /public|private|unsafe/i);
  assert.throws(() => validateResolvedRemoteAddresses([]), /address|DNS/i);
});

test('signed JSON rejects loopback without opening any HTTP connection', async () => {
  let requests = 0;
  const server = createServer((_req, res) => { requests++; res.writeHead(200).end('ok'); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const address = server.address();
    const {privateKey} = await crypto.subtle.generateKey({name:'RSASSA-PKCS1-v1_5', modulusLength:2048, publicExponent:new Uint8Array([1,0,1]), hash:'SHA-256'}, true, ['sign','verify']);
    await assert.rejects(() => sendSignedJsonActivity('test', `http://127.0.0.1:${address.port}/inbox`, {keyId: new URL('https://bridge.example/key'), privateKey}, {id:'https://bridge.example/announces/1', type:'Announce'}, {debugDelivery:false}), /https|private|loopback|public/i);
    assert.equal(requests, 0);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
});
