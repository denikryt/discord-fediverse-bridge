/** Direct remote Person root/Update identity and Group regression scenarios. */
import assert from "node:assert/strict";
import { normalizeDirectRemotePersonEventFromJson, normalizeUpdateActivityFromJson } from "../src/normalize.js";

const ACTOR = "https://mastodon.example/users/bob";
const NOTE = `${ACTOR}/statuses/7`;
const PUBLIC = "https://www.w3.org/ns/activitystreams#Public";
const note = (extra: Record<string, unknown> = {}) => ({
  type: "Note", id: NOTE, attributedTo: ACTOR,
  content: "Public post", to: [PUBLIC], published: "2026-11-16T01:00:00Z", ...extra,
});
const activity = (type: "Create" | "Update", extra: Record<string, unknown> = {}) => ({
  type, id: `${ACTOR}/activities/${type.toLowerCase()}/7`, actor: ACTOR,
  object: note(), to: [PUBLIC], ...extra,
});

async function main(): Promise<void> {
  const create = normalizeDirectRemotePersonEventFromJson(activity("Create"));
  assert.equal(create?.event_type, "remote.person.created");
  assert.equal(create?.actor_id, ACTOR);
  assert.equal(create?.object.ap_id, NOTE);
  assert.equal(create?.object.body_markdown, "Public post");

  // Fedify's typed Note.toJsonLd() compacts Public to as:Public. The
  // forwarded Python proof must still contain the canonical public IRI.
  const compact = normalizeDirectRemotePersonEventFromJson(activity("Create", {
    to: ["as:Public"], object: note({ to: ["as:Public"] }),
  }));
  assert.equal(compact?.event_type, "remote.person.created");
  assert.deepEqual(compact?.source_activity_json.to, [PUBLIC]);
  assert.deepEqual((compact?.source_activity_json.object as Record<string, unknown>).to, [PUBLIC]);

  const update = await normalizeUpdateActivityFromJson(activity("Update"));
  assert.equal(update?.event_type, "remote.person.updated");
  assert.equal(update?.object.ap_id, NOTE);

  // A forged attribution and followers-only post must never create work.
  assert.equal(normalizeDirectRemotePersonEventFromJson(activity("Create", {
    object: note({ attributedTo: "https://bad.example/user" }),
  })), null);
  assert.equal(normalizeDirectRemotePersonEventFromJson(activity("Create", {
    object: note({ to: [`${ACTOR}/followers`] }),
  })), null);

  // Roots require Public in the explicit `to` of BOTH Note and Create;
  // public-only `cc` is accepted for replies, not root broadcasts.
  assert.equal(normalizeDirectRemotePersonEventFromJson(activity("Create", {
    to: [], cc: [PUBLIC], object: note({ to: [], cc: [PUBLIC] }),
  })), null);
  // Delete can reference the original object by IRI without a nested Note.
  const deletion = normalizeDirectRemotePersonEventFromJson({
    type: "Delete", id: `${ACTOR}/activities/delete/7`, actor: ACTOR, object: NOTE,
  });
  assert.equal(deletion?.event_type, "remote.person.deleted");
  assert.equal(deletion?.object.ap_id, NOTE);

  // Group targeting remains the NodeBB/Lemmy path; wrapping does not prove
  // that the inner Person independently signed the Create.
  const group = "https://nodebb.example/category/17";
  assert.equal(normalizeDirectRemotePersonEventFromJson(activity("Create", {
    object: note({ audience: group }),
  })), null);
  assert.equal(normalizeDirectRemotePersonEventFromJson(activity("Create", {
    object: note({ inReplyTo: `${ACTOR}/statuses/1` }),
  })), null);
  console.log("remote Person root normalization passed");
}

await main();

// Note.name must not be presented as its author identity.
const titledDirect = normalizeDirectRemotePersonEventFromJson({
  type: 'Create', id: 'https://remote.example/create/titled',
  actor: 'https://remote.example/users/bob', to: [PUBLIC],
  object: { type: 'Note', id: 'https://remote.example/notes/titled',
    attributedTo: 'https://remote.example/users/bob',
    name: 'Important update', to: [PUBLIC], content: 'Hello', },
});
assert.equal(titledDirect?.object.author_name, 'bob');
