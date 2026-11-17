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

// A public reply may address recipients through cc, unlike a root Note.
const ccOnly = normalizeDirectRemotePersonCommentFromJson({
  ...signed, to: [], cc: ['as:Public'],
  object: { ...signed.object, to: [], cc: ['as:Public'] },
});
assert.equal(ccOnly?.event_type, 'remote.person.comment.created');
assert.deepEqual(ccOnly?.source_activity_json.cc, [publicAudience]);
const deleted = normalizeDirectRemotePersonCommentFromJson({
  type: 'Delete', id: 'https://example.org/delete/1',
  actor: signed.actor, object: signed.object.id,
});
assert.equal(deleted?.event_type, 'remote.person.comment.deleted');
assert.equal(deleted?.object.ap_id, signed.object.id);
assert.equal(deleted?.object.parent_ap_id, null);
assert.equal(normalizeDirectRemotePersonCommentFromJson({
  ...signed, object: { ...signed.object, audience: 'https://example.org/category/17' },
}), null);
