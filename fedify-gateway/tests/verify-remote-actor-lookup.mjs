/** Scenario tests: remote Actor read happens through the hardened federation context. */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { registerHooks } from 'node:module';
registerHooks({resolve(specifier, context, next) {
  if (specifier.startsWith('.') && specifier.endsWith('.js') && context.parentURL?.includes('/fedify-gateway/src/')) {
    return next(specifier.replace(/\.js$/, '.ts'), context);
  }
  return next(specifier, context);
}});
const { fetchRemoteCommunity } = await import('../src/outbound/remote-actor.ts');

test('a verified public remote Actor returns canonical identity and allowed inbox', async () => {
  const seen = [];
  const context = { async lookupObject(url, options) {
    seen.push([url, options]);
    return { id:new URL(url), inboxId:new URL('https://relay.example/inbox') };
  }};
  assert.deepEqual(await fetchRemoteCommunity(context, 'https://relay.example/c/1'), {
    communityId:'https://relay.example/c/1', inboxUrl:'https://relay.example/inbox'
  });
  assert.deepEqual(seen, [['https://relay.example/c/1', {crossOrigin:'throw'}]]);
});

test('rejects unsafe actor URL before invoking lookupObject', async () => {
  let reads = 0;
  const context = {async lookupObject() { reads++; return null; }};
  for (const url of ['http://relay.example/c/1', 'https://localhost/c/1', 'https://127.0.0.1/c/1', 'https://user:secret@relay.example/c/1']) {
    await assert.rejects(() => fetchRemoteCommunity(context, url), /https|private|public|credential/i);
  }
  assert.equal(reads, 0);
});

test('rejects mismatched Actor identity and private inbox from remote document', async () => {
  const contexts = [
    {async lookupObject() {return {id:new URL('https://attacker.example/a'),inboxId:new URL('https://relay.example/inbox')};}},
    {async lookupObject() {return {id:new URL('https://relay.example/c/1'),inboxId:new URL('http://169.254.169.254/meta')};}},
    {async lookupObject() {return {id:new URL('https://relay.example/c/1'),inboxId:null};}},
  ];
  for (const context of contexts) {
    await assert.rejects(() => fetchRemoteCommunity(context, 'https://relay.example/c/1'), /identity|inbox|https/i);
  }
});
