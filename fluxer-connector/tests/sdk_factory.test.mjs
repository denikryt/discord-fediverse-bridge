/** Trusted SDK client construction is observable without importing unavailable npm SDK. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {createTrustedSdkClient} from '../src/sdk_factory.ts';

/** All endpoint fields required by the pinned SDK must come from this instance. */
function officialSdkEndpoints() {
  return Object.fromEntries([
    'api','api_client','media','static_cdn','marketing','admin','invite',
    'gift','webapp',
  ].map(name=>[name,`https://${name}.example.test`]));
}

test('SDK client inherits vetted API/Gateway endpoints without another discovery fetch', () => {
  const built=[];
  class FakeClient {
    constructor(options) {built.push(options);this.options=options;}
  }
  const discovery={origin:'https://example.test', apiPublic:'https://api.example.test/',
    gateway:'wss://gateway.example.test/', endpoints:officialSdkEndpoints()};
  const client=createTrustedSdkClient(discovery, FakeClient);
  assert.equal(built.length,1);
  assert.equal(client.options.instance.api_public,'https://api.example.test/');
  assert.equal(client.options.instance.gateway,'wss://gateway.example.test/');
  assert.equal(client.options.instance.api,'https://api.example.test');
  assert.equal(client.options.instance.webapp,'https://webapp.example.test');
  assert.equal(client.options.rest.retries,0);
  // Bad /gateway/bot response cannot open a WebSocket to another hostname.
  assert.throws(() => new client.options.WebSocket(
    'wss://foreign.example.test/?v=1&encoding=json'), /unapproved/);
});

test('factory refuses malformed API/Gateway endpoints before constructing SDK', () => {
  let count=0;
  class FakeClient {constructor() {count++;}}
  for (const discovery of [
    {origin:'https://example.test',apiPublic:'http://api.example.test/',
      gateway:'wss://gateway.example.test/'},
    {origin:'https://example.test',apiPublic:'https://api.example.test/',
      gateway:'wss://username:secret@gateway.example.test/'},
  ]) assert.throws(() => createTrustedSdkClient(discovery, FakeClient));
  assert.equal(count,0);
});

test('missing SDK discovery endpoint refuses before Client construction, not hosted defaults', () => {
  let attempts=0;
  class FakeClient {constructor() {attempts++;}}
  const endpoints=officialSdkEndpoints();
  delete endpoints.admin;
  assert.throws(() => createTrustedSdkClient({
    origin:'https://example.test',apiPublic:'https://api.example.test',
    gateway:'wss://gateway.example.test',endpoints,
  },FakeClient),/missing Fluxer SDK discovery endpoint/);
  assert.equal(attempts,0);
});
