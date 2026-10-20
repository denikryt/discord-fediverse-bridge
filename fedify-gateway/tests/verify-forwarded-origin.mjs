/** Scenario regression: only origin-verified nested ActivityPub source enters ingress. */
import assert from 'node:assert/strict';
import { test } from 'node:test';
const { verifyForwardedSourceRecord } = await import('../src/verified-forwarded-source.ts');

const actor = 'https://author.example/users/bob';
const create = {
  type:'Create', id:'https://author.example/activities/create-1', actor,
  object: {type:'Note', id:'https://author.example/notes/1', attributedTo:actor,
    content:'original body', published:'2026-10-19T00:00:00Z',
    inReplyTo:'https://board.example/posts/1',
    audience:'https://board.example/c/one',
    to:['https://www.w3.org/ns/activitystreams#Public']},
};
const wrapper = {type:'Announce', id:'https://relay.example/announce/1', actor:'https://relay.example/c/one', object:create};
const observed = {id:wrapper.id, actorId:wrapper.actor};
const lookup = async id => id === create.id ? structuredClone(create) : null;

test('a different relayer may forward a byte-equivalent author-hosted Create', async () => {
  const checked = await verifyForwardedSourceRecord(observed, wrapper, create, lookup);
  assert.deepEqual(checked, create);
});

test('an outer Announce without author-origin proof is rejected', async () => {
  await assert.rejects(() => verifyForwardedSourceRecord(observed, wrapper, create, async ()=>null), /origin|verified|retriev|unverif/i);
});

test('forged author, payload, object ID, parent or audience cannot use valid relayer signature', async () => {
  const variants = [
    {actor: 'https://author.example/users/carol'},
    {object:{...create.object, content:'attacker edit'}},
    {object:{...create.object, id:'https://author.example/notes/evil'}},
    {object:{...create.object, inReplyTo:'https://board.example/posts/evil'}},
    {object:{...create.object, audience:'https://board.example/c/evil'}},
    {object:{...create.object, attributedTo:'https://author.example/users/carol'}},
  ];
  for (const mutation of variants) {
    const nested = {...create, ...mutation};
    await assert.rejects(() => verifyForwardedSourceRecord(observed, {...wrapper, object:nested}, nested, lookup), /origin|identity|mismatch|verified|author|payload/i);
  }
});

test('cache ID collision or mismatched envelope author is rejected before dereference', async () => {
  let reads=0;
  const checked = async () => {reads++; return structuredClone(create);};
  const variants = [
    {...wrapper, id:'https://relay.example/announce/other'},
    {...wrapper, actor:'https://relay.example/c/other'},
    {...wrapper, object:{...create, id:'https://author.example/activities/other'}},
  ];
  for (const raw of variants) await assert.rejects(() => verifyForwardedSourceRecord(observed, raw, create, checked));
  assert.equal(reads, 0);
});

test('origin-verified Update cannot be substituted with an old Create', async () => {
  const update = {...create, type:'Update', id:'https://author.example/activities/update-1',
    object:{...create.object, content:'revised', updated:'2026-10-19T01:00:00Z'}};
  const envelope = {...wrapper, object:update};
  const fetched = async () => structuredClone(update);
  assert.deepEqual(await verifyForwardedSourceRecord(observed, envelope, update, fetched), update);
  await assert.rejects(() => verifyForwardedSourceRecord(observed, envelope, update, async()=>structuredClone(create)));
});

test('wrapped Delete requires its own original mutation, not proof of Create', async () => {
  const del = {type:'Delete', id:'https://author.example/activities/delete-1', actor,
    object:'https://author.example/notes/1'};
  const envelope = {...wrapper, object:del};
  assert.deepEqual(await verifyForwardedSourceRecord(observed, envelope, del, async()=>del), del);
  await assert.rejects(() => verifyForwardedSourceRecord(observed, envelope, del, lookup));
});

const { verifyDirectSourceAttribution } = await import('../src/verified-forwarded-source.ts');
test('authenticated direct Create with matching author/object reaches normalization', () => {
  assert.deepEqual(verifyDirectSourceAttribution({id:create.id, actorId:actor}, create), create);
});
test('authenticated direct Create rejects object attribution or actor substitution', () => {
  const tampered = {...create, object:{...create.object, attributedTo:'https://author.example/users/carol'}};
  assert.throws(() => verifyDirectSourceAttribution({id:create.id, actorId:actor}, tampered));
  assert.throws(() => verifyDirectSourceAttribution({id:create.id, actorId:actor}, {...create, actor:'https://author.example/users/carol'}));
});
