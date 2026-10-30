/** Observable SDK session lifecycle scenarios using a deterministic provider boundary. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {SdkSession} from '../src/sdk_session.ts';

const origin = 'https://example.test';
const apiPublic = 'https://api.example.test/';
const options = {origin, apiPublic, botToken: 'private-bot-token', readyTimeoutMs: 150};
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
}
function provider({endpoint = apiPublic, bot = true, loginError = false,
                   autoReady = true, loginGate} = {}) {
  const events = new EventEmitter();
  const calls = [];
  const client = {
    on: events.on.bind(events),
    off: events.off.bind(events),
    instance: {endpoints: {api_public: endpoint}},
    user: undefined,
    async login(token) {
      calls.push(['login', token]);
      if (loginGate) await loginGate.promise;
      if (loginError) throw new Error('provider sensitive token detail');
      client.user = {id: 'bot-777', bot};
      if (autoReady) events.emit('ready');
    },
    async destroy() {calls.push(['destroy']);},
  };
  return {client, calls, events, createClient: async () => client};
}

test('verified self-hosted SDK login publishes readiness only after bot identity and READY', async () => {
  const remote = provider();
  const session = new SdkSession({...options, createClient: remote.createClient});
  assert.equal(session.health().state, 'new');
  await session.start();
  assert.deepEqual(session.health(), {state: 'ready', connector_key: undefined,
    bot_id: 'bot-777', gateway_gap: false});
  assert.deepEqual(remote.calls, [['login', options.botToken]]);
  remote.events.emit('disconnect');
  assert.equal(session.health().state, 'degraded');
  remote.events.emit('ready');
  assert.equal(session.health().state, 'ready');
  await session.close();
  assert.equal(session.health().state, 'stopped');
  assert.deepEqual(remote.calls.at(-1), ['destroy']);
  assert.equal(remote.events.listenerCount('ready'), 0);
});

test('an unapproved discovered API endpoint cannot receive the bot token', async () => {
  const remote = provider({endpoint: 'https://api.attacker.test/'});
  const session = new SdkSession({...options, createClient: remote.createClient});
  await assert.rejects(session.start(), /SDK initialization refused/);
  assert.deepEqual(remote.calls, [['destroy']]);
  assert.equal(session.health().state, 'stopped');
});

test('user without positive bot identity is not advertised ready', async () => {
  const remote = provider({bot: false});
  const session = new SdkSession({...options, createClient: remote.createClient});
  await assert.rejects(session.start(), /SDK initialization refused/);
  assert.equal(session.health().state, 'stopped');
  assert.deepEqual(remote.calls.at(-1), ['destroy']);
});

test('login failures redact provider error and release client resources', async () => {
  const remote = provider({loginError: true});
  const session = new SdkSession({...options, createClient: remote.createClient});
  await assert.rejects(session.start(), error =>
    error.message === 'Fluxer SDK initialization refused');
  assert.deepEqual(remote.calls.at(-1), ['destroy']);
});

test('missing READY times out and does not leave a logged-in Gateway', async () => {
  const remote = provider({autoReady: false});
  const session = new SdkSession({...options, readyTimeoutMs: 30,
    createClient: remote.createClient});
  await assert.rejects(session.start(), /SDK initialization refused/);
  assert.deepEqual(remote.calls.at(-1), ['destroy']);
  assert.equal(session.health().state, 'stopped');
});

test('concurrent startup and shutdown joins pending login then disposes once', async () => {
  const gate = deferred();
  const remote = provider({loginGate: gate});
  const session = new SdkSession({...options, createClient: remote.createClient});
  const starting = session.start();
  // Reaching SDK login proves a remote session can already be alive.
  await new Promise(resolve => setImmediate(resolve));
  const closing = session.close();
  gate.resolve();
  await assert.rejects(starting, /SDK initialization refused/);
  await closing;
  await session.close();
  assert.equal(remote.calls.filter(([name]) => name === 'destroy').length, 1);
  assert.equal(session.health().state, 'stopped');
});

test('invalid credentials and configuration are rejected before client construction', async () => {
  let created = 0;
  for (const edit of [{botToken: ''}, {readyTimeoutMs: -1}, {origin: 'http://evil.test'}]) {
    assert.throws(() => new SdkSession({...options, ...edit,
      createClient: async () => {created++; return provider().client;}}));
  }
  assert.equal(created, 0);
});

test('explicit loopback development permits HTTP only at the selected local API', async () => {
  const remote = provider({endpoint: 'http://127.0.0.1:8771/'});
  const session = new SdkSession({...options, origin: 'http://127.0.0.1:8771',
    apiPublic: 'http://127.0.0.1:8771/', allowInsecureLocalDevelopment: true,
    createClient: remote.createClient});
  await session.start();
  assert.equal(session.health().state, 'ready');
  await session.close();
  assert.throws(() => new SdkSession({...options, apiPublic: 'http://api.example.test/',
    allowInsecureLocalDevelopment: true, createClient: remote.createClient}));
});

test('invalid Gateway session marks a visible reconnect gap without event replay claims', async () => {
  const remote = provider();
  const session = new SdkSession({...options, createClient: remote.createClient});
  await session.start();
  remote.events.emit('invalidSession');
  assert.equal(session.health().state, 'degraded');
  assert.equal(session.health().gateway_gap, true);
  remote.events.emit('ready');
  assert.equal(session.health().state, 'ready');
  assert.equal(session.health().gateway_gap, true);
  await session.close();
});

test('teardown failure cannot expose the provider error text during failed login', async () => {
  const remote = provider({loginError: true});
  remote.client.destroy = async () => {throw new Error('very-secret-provider-diagnostic');};
  const session = new SdkSession({...options, createClient: remote.createClient});
  await assert.rejects(session.start(), error =>
    error.message === 'Fluxer SDK initialization refused');
});

test('failure to destroy SDK does not falsely mark the session stopped', async () => {
  const remote = provider();
  let failures = 1;
  remote.client.destroy = async () => {
    if (failures--) throw new Error('sensitive sdk failure');
    remote.calls.push(['destroy']);
  };
  const session = new SdkSession({...options, createClient: remote.createClient});
  await session.start();
  await assert.rejects(session.close(), error =>
    error.message === 'Fluxer SDK teardown incomplete');
  assert.equal(session.health().state, 'degraded');
  await session.close();
  assert.equal(session.health().state, 'stopped');
});

test('SDK REST baseUrl may not override validated api_public with a first-party host', async () => {
  const remote = provider();
  remote.client.rest = {baseUrl: 'https://web.example.test/api/v1'};
  const session = new SdkSession({...options, createClient: remote.createClient});
  await assert.rejects(session.start(), /SDK initialization refused/);
  assert.deepEqual(remote.calls, [['destroy']]);
});

test('READY emitted after login resolution also leads to authenticated readiness', async () => {
  const remote = provider({autoReady: false});
  const session = new SdkSession({...options, createClient: remote.createClient});
  const started = session.start();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(session.health().state, 'connecting');
  remote.events.emit('ready');
  await started;
  assert.equal(session.health().bot_id, 'bot-777');
  await session.close();
});

test('READY followed by disconnect before login completes cannot advertise readiness', async () => {
  const gate = deferred();
  const remote = provider({loginGate: gate, autoReady: false});
  const session = new SdkSession({...options, createClient: remote.createClient});
  const starting = session.start();
  await new Promise(resolve => setImmediate(resolve));
  remote.events.emit('ready');
  remote.events.emit('disconnect');
  gate.resolve();
  await new Promise(resolve => setImmediate(resolve));
  assert.notEqual(session.health().state, 'ready');
  remote.events.emit('ready');
  await starting;
  assert.equal(session.health().state, 'ready');
  await session.close();
});

test('self-hosted bot API base paths survive the SDK endpoint identity check', async () => {
  const remote = provider({endpoint: 'https://api.example.test/bot/' });
  remote.client.rest = {baseUrl: 'https://api.example.test/bot/v1'};
  const session = new SdkSession({...options, apiPublic: 'https://api.example.test/bot/',
    createClient: remote.createClient});
  await session.start();
  assert.equal(session.health().state, 'ready');
  await session.close();
});
