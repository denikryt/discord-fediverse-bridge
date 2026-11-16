/** Exercise the actual TS Person renderer and public reconstruction without Node dependencies. */
import assert from 'node:assert/strict';
import { renderPersonalFeedCreate } from '../../fedify-gateway/src/outbound/personal-feed-renderer.ts';
import { buildPublishedActivityObjectJson, buildPublishedCreateActivityJson } from '../../fedify-gateway/src/published-objects.ts';
const origin = 'https://bridge.example';
const actor = `${origin}/actors/users/alice`;
const bob = 'https://remote.example/users/bob';
const operationId = 'a'.repeat(64);
const objectId = `${actor}/objects/comments/${operationId}`;
const activityId = `${actor}/activities/create/comments/${operationId}`;
const inReplyToObjectId = 'https://remote.example/notes/root';
const request = { actorUsername:'alice', operationId, objectId, activityId,
  bodyMarkdown:'A <test>', publishedAt:'2026-11-16T00:00:00.000Z',
  kind:'comment', inReplyToObjectId, directPersonActorIds:[bob] };
const { activityJson: rendered } = renderPersonalFeedCreate({fedifyOrigin:origin},request);
assert.deepEqual(rendered.object.cc, [`${actor}/followers`,bob]);
assert.deepEqual(rendered.cc, [actor,`${actor}/followers`,bob]);
const row = { actorUsername:'alice',actorUrl:actor,publicationContext:'personal_feed',
  activityId,objectId,kind:'comment',bodyMarkdown:request.bodyMarkdown,
  inReplyToObjectId,publishedAt:request.publishedAt,directPersonActorIds:[bob] };
const object = buildPublishedActivityObjectJson(row);
const create = buildPublishedCreateActivityJson(row);
assert.deepEqual(create.cc,rendered.cc);
assert.deepEqual(object.cc,rendered.object.cc);
assert.equal(object.inReplyTo,inReplyToObjectId);
assert.equal(create.object.inReplyTo,inReplyToObjectId);
assert.throws(() => renderPersonalFeedCreate({fedifyOrigin:origin}, {...request, directPersonActorIds:[bob,bob]}),/unique/);
assert.throws(() => renderPersonalFeedCreate({fedifyOrigin:origin}, {...request, directPersonActorIds:['http://127.0.0.1/inbox']}),/invalid/);
console.log('Person Create/direct audience renderer: passed');
