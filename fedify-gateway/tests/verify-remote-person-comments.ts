/** Regression: authenticated remote reply emits no community/Lemmy identity. */
import assert from 'node:assert/strict';
import { normalizeDirectRemotePersonCommentFromJson } from '../src/normalize.js';

const publicAudience = 'https://www.w3.org/ns/activitystreams#Public';
const signed = {
  id: 'https://example.org/create/1', type: 'Create',
  actor: 'https://example.org/users/alice', to: [publicAudience],
  object: { id: 'https://example.org/notes/1', type: 'Note',
    attributedTo: 'https://example.org/users/alice',
    inReplyTo: 'https://remote.test/notes/bob', to: [publicAudience],
    content: 'Hello',
  },
};
const event = normalizeDirectRemotePersonCommentFromJson(signed);
assert.equal(event?.event_type, 'remote.person.comment.created');
assert.equal(event?.object.parent_ap_id, 'https://remote.test/notes/bob');
const compact = normalizeDirectRemotePersonCommentFromJson({
  ...signed, to: ['as:Public'], object: { ...signed.object, to: ['as:Public'] },
});
assert.equal(compact?.event_type, 'remote.person.comment.created');
assert.deepEqual(compact?.source_activity_json.to, [publicAudience]);
assert.equal(normalizeDirectRemotePersonCommentFromJson({
  ...signed, object: { ...signed.object, attributedTo: 'https://example.org/users/eve' },
}), null);
assert.equal(normalizeDirectRemotePersonCommentFromJson({
  ...signed, to: [],
}), null);
assert.equal(normalizeDirectRemotePersonCommentFromJson({
  ...signed, object: { ...signed.object, inReplyTo: null },
}), null);
