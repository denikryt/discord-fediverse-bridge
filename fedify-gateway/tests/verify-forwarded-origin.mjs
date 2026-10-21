/** Scenario regression: only origin-verified nested ActivityPub source enters ingress. */
import assert from 'node:assert/strict';
import { test } from 'node:test';
const { verifyForwardedSourceRecord } = await import('../src/verified-forwarded-source.ts');
const { storeRawActivity, getRawActivity } = await import('../src/activitypub-raw-cache.ts');
const { Announce } = await import('@fedify/vocab');

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
const observed = {id:wrapper.id, actorId:wrapper.actor, objectId:create.id, objectPayload:create};
const lookup = async id => id === create.id ? structuredClone(create) : null;

test('a different relayer may forward a byte-equivalent author-hosted Create', async () => {
  const checked = await verifyForwardedSourceRecord(observed, wrapper, create, lookup);
  assert.deepEqual(checked, create);
});

test('equivalent delivery profiles retain one author source despite JSON order and timestamp formatting', async () => {
  // The relay and author can serialize the same object differently. The
  // origin record remains authoritative when the compared meaning is equal.
  const canonical = {
    actor, id:create.id, type:'Create',
    object:{
      to:['https://www.w3.org/ns/activitystreams#Public'],
      audience:create.object.audience,
      inReplyTo:create.object.inReplyTo,
      published:'2026-10-19T03:00:00+03:00',
      content:create.object.content,
      attributedTo:actor,
      id:create.object.id,
      type:'Note',
    },
  };
  assert.deepEqual(
    await verifyForwardedSourceRecord(observed, wrapper, create, async()=>canonical),
    canonical,
  );
});

test('the installed Fedify parser preserves the accepted nested activity ID', async () => {
  // Exercise the SDK representation used by the actual inbox listener, not an
  // independently supplied identity that the application could invent.
  const typed = await Announce.fromJsonLd({
    '@context':'https://www.w3.org/ns/activitystreams', ...wrapper,
  });
  assert.equal(typed.objectId?.href, create.id);
  const acceptedJson = await typed.toJsonLd();
  const accepted = {id:typed.id?.href, actorId:typed.actorId?.href,
    objectId:typed.objectId?.href, objectPayload:acceptedJson.object};
  assert.deepEqual(await verifyForwardedSourceRecord(accepted, wrapper, create, lookup), create);
  await assert.rejects(() => verifyForwardedSourceRecord(
    {...accepted, objectId:null}, wrapper, create, lookup,
  ), /identity|mismatch/i);
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

test('same-ID cache replacement cannot change the Fedify-accepted nested content', async () => {
  // A later request may reuse both outer and nested IDs, so comparing IDs
  // alone cannot establish which Create body the signed request contained.
  const substituted = {...create, object:{...create.object, content:'replacement'}};
  let originReads = 0;
  await assert.rejects(() => verifyForwardedSourceRecord(
    {...observed, objectPayload:create},
    {...wrapper, object:substituted}, substituted,
    async () => { originReads++; return substituted; },
  ), /accepted|signed|payload|mismatch/i);
  assert.equal(originReads, 0);
});

test('a later unsigned inbox body cannot substitute a different Create under an accepted Announce ID', async () => {
  // Raw capture occurs before Fedify authenticates an inbox POST. A second
  // sender can reuse the signed envelope ID while the first job is queued.
  storeRawActivity(wrapper.id, wrapper);
  const forged = {
    ...create,
    id:'https://attacker.example/activities/create-1',
    actor:'https://attacker.example/users/mallory',
    object:{...create.object, id:'https://attacker.example/notes/1',
      attributedTo:'https://attacker.example/users/mallory'},
  };
  storeRawActivity(wrapper.id, {...wrapper, object:forged});
  const cached = getRawActivity(wrapper.id)?.rawJson;
  let originReads = 0;
  await assert.rejects(() => verifyForwardedSourceRecord(
    observed, cached, cached.object,
    async () => {originReads++; return forged;},
  ), /accepted|signed|identity|mismatch/i);
  assert.equal(originReads, 0, 'the substituted original must not be dereferenced');
});

for (const kind of ['Update', 'Delete']) {
  test(`a same-ID raw cache collision cannot substitute an embedded ${kind}`, async () => {
    // A mutation must remain bound to the typed activity Fedify actually
    // accepted, even when the hostile source hosts a valid original mutation.
    const original = {type:kind, id:`https://author.example/activities/${kind.toLowerCase()}-1`,
      actor, object:kind === 'Delete' ? create.object.id : create.object};
    const hostileActor = 'https://attacker.example/users/mallory';
    const substituted = {type:kind, id:`https://attacker.example/activities/${kind.toLowerCase()}-1`,
      actor:hostileActor, object:kind === 'Delete'
        ? 'https://attacker.example/notes/1'
        : {...create.object, id:'https://attacker.example/notes/1', attributedTo:hostileActor}};
    storeRawActivity(wrapper.id, {...wrapper, object:original});
    storeRawActivity(wrapper.id, {...wrapper, object:substituted});
    const cached = getRawActivity(wrapper.id)?.rawJson;
    let originReads = 0;
    await assert.rejects(() => verifyForwardedSourceRecord(
      {...observed, objectId:original.id}, cached, cached.object,
      async () => {originReads++; return substituted;},
    ), /accepted|signed|identity|mismatch/i);
    assert.equal(originReads, 0);
  });
}

test('origin-verified Update cannot be substituted with an old Create', async () => {
  const update = {...create, type:'Update', id:'https://author.example/activities/update-1',
    object:{...create.object, content:'revised', updated:'2026-10-19T01:00:00Z'}};
  const envelope = {...wrapper, object:update};
  const fetched = async () => structuredClone(update);
  assert.deepEqual(await verifyForwardedSourceRecord({...observed, objectId:update.id, objectPayload:update}, envelope, update, fetched), update);
  await assert.rejects(() => verifyForwardedSourceRecord({...observed, objectId:update.id, objectPayload:update}, envelope, update, async()=>structuredClone(create)));
});

test('wrapped Delete requires its own original mutation, not proof of Create', async () => {
  const del = {type:'Delete', id:'https://author.example/activities/delete-1', actor,
    object:'https://author.example/notes/1'};
  const envelope = {...wrapper, object:del};
  assert.deepEqual(await verifyForwardedSourceRecord({...observed, objectId:del.id, objectPayload:del}, envelope, del, async()=>del), del);
  await assert.rejects(() => verifyForwardedSourceRecord({...observed, objectId:del.id, objectPayload:del}, envelope, del, lookup));
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
