/** Canonical public Person Notes remain source-independent across feed modes.
 * This renderer-only test runs on Node 22 without Fedify dependencies.
 */
import assert from "node:assert/strict";
import { buildPublishedActivityObjectJson, buildPublishedCreateActivityJson } from "../src/published-objects.js";
import { buildUserOutboxJson } from "../src/user-outbox.js";

const actor = "https://bridge.test/actors/users/alice";
const remoteParent = "https://remote.test/users/bob/statuses/7";
const bob = "https://remote.test/users/bob";
const record = {
  actorUsername: "alice", actorUrl: actor, publicationContext: "remote_person_feed" as const,
  communityActorUrl: null, activityId: `${actor}/activities/create/comments/${"a".repeat(64)}`,
  objectId: `${actor}/objects/comments/${"a".repeat(64)}`,
  kind: "comment" as const, title: null, bodyMarkdown: "Hi <b>Bob</b>",
  inReplyToObjectId: remoteParent, publishedAt: "2026-11-16T00:00:00.000Z",
  directPersonActorIds: [bob], discordGuildId: 1, discordChannelId: 100, discordMessageId: 777,
};
const object = buildPublishedActivityObjectJson(record);
const create = buildPublishedCreateActivityJson(record);
assert.equal(object.type, "Note");
assert.equal(object.inReplyTo, remoteParent);
assert.deepEqual(object.to, ["https://www.w3.org/ns/activitystreams#Public"]);
assert.deepEqual(object.cc, [`${actor}/followers`, bob]);
assert.equal(object.attributedTo, actor);
assert.match(String(object.content), /&lt;b&gt;Bob&lt;\/b&gt;/);
assert.deepEqual(create.object, object);
assert.deepEqual(create.cc, [actor, `${actor}/followers`, bob]);
assert.equal(create.id, record.activityId);
const outbox = buildUserOutboxJson({
  outboxUrl: `${actor}/outbox`, totalItems: 1, page: 1, pageSize: 20,
  items: [record],
}, 1);
assert.deepEqual(outbox.orderedItems, [create]);

// Original Personal Feed and Group publications retain their established shapes.
const personal = buildPublishedActivityObjectJson({ ...record, publicationContext: "personal_feed" });
assert.deepEqual(personal, object);
assert.throws(() => buildPublishedCreateActivityJson({
  ...record, publicationContext: "community", communityActorUrl: null,
}), /community publication is missing/);
const group = buildPublishedCreateActivityJson({
  ...record, publicationContext: "community", communityActorUrl: "https://bridge.test/actors/communities/tech",
});
assert.equal((group.object as Record<string, unknown>).audience, "https://bridge.test/actors/communities/tech");
assert.equal((group.object as Record<string, unknown>).type, "Note");
console.log("remote Person public object/Create/outbox context invariants: OK");
