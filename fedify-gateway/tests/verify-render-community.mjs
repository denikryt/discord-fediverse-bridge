/** Native Node, dependency-free proof that Create construction has no network effects. */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { renderCommunityCreate } from '../src/outbound/community-renderer.ts';

const OP = 'a'.repeat(64);
const config = { fedifyOrigin: 'https://bridge.test' };
const root = {
  actorUsername: 'alice', communityActorUrl: 'https://remote.test/c/news',
  kind: 'post', title: 'Hello', bodyMarkdown: 'test\n\nline 2',
  inReplyToObjectId: null, operationId: OP,
  activityId: `https://bridge.test/actors/users/alice/activities/create/posts/${OP}`,
  objectId: `https://bridge.test/actors/users/alice/objects/posts/${OP}`,
  publishedAt: '2026-10-23T10:00:00.000Z',
};

test('same source renders byte-stable Page without any transport', () => {
  const one = renderCommunityCreate(config, root);
  const two = renderCommunityCreate(config, root);
  assert.deepEqual(one, two);
  assert.equal(one.activityJson.object.type, 'Page');
  assert.equal(one.activityJson.object.published, root.publishedAt);
  assert.equal(one.activityJson.object.name, 'Hello');
  assert.ok(one.activityJson.to.includes(root.communityActorUrl));
});

test('comment preserves exact AP parent and stable Note', () => {
  const post = { ...root, kind: 'comment', title: null,
    activityId: root.activityId.replace('/posts/', '/comments/'),
    objectId: root.objectId.replace('/posts/', '/comments/'),
    inReplyToObjectId: 'https://remote.test/o/parent' };
  const out = renderCommunityCreate(config, post);
  assert.equal(out.activityJson.object.type, 'Note');
  assert.equal(out.activityJson.object.inReplyTo, post.inReplyToObjectId);
});

test('bad identity and guessed publication time fail closed', () => {
  for (const corrupt of [{ activityId: 'https://other.test/a/fake' },
                         { operationId: 'not-sha256' },
                         { publishedAt: '2026-10-23T10:00:00Z' },
                         { communityActorUrl: 'http://remote.test/c/news' }]) {
    assert.throws(() => renderCommunityCreate(config, { ...root, ...corrupt }));
  }
  assert.throws(() => renderCommunityCreate(config, { ...root, kind: 'comment' }));
});
