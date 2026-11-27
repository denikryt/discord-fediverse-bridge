/** Verify Person Note mutation identity and original nonfollower audience. */
import assert from "node:assert/strict";
import { updateContent, deleteContent } from "../src/outbound/content-mutations.js";

const config = { fedifyOrigin: "https://bridge.test" } as never;
const actor = "https://bridge.test/actors/users/alice";
const bob = "https://remote.example/users/bob";
const parent = "https://remote.example/users/bob/statuses/1";
const objectId = `${actor}/objects/comments/${"a".repeat(64)}`;
const request = {
  actorUsername: "alice", publicationContext: "remote_person_feed" as const,
  communityActorUrl: null, apObjectId: objectId,
  inReplyToObjectId: parent, directPersonActorIds: [bob],
};
const updated = await updateContent({} as never, config, {
  ...request, kind: "comment", bodyMarkdown: "An edited remote discussion answer",
});
const update = updated.activityJson;
assert.equal(update.type, "Update");
assert.equal(update.actor, actor);
assert.deepEqual(update.cc, [actor, `${actor}/followers`, bob]);
assert.equal((update.object as Record<string, unknown>).type, "Note");
assert.equal((update.object as Record<string, unknown>).inReplyTo, parent);
assert.deepEqual((update.object as Record<string, unknown>).cc, [`${actor}/followers`, bob]);
const deleted = await deleteContent({} as never, config, request);
assert.equal(deleted.activityJson.type, "Delete");
assert.equal(deleted.activityJson.object, objectId);
assert.deepEqual(deleted.activityJson.cc, [actor, `${actor}/followers`, bob]);
await assert.rejects(updateContent({} as never, config, {
  ...request, communityActorUrl: "https://remote.example/groups/forbidden",
  kind: "comment", bodyMarkdown: "invalid",
}), /communityActorUrl/);
await assert.rejects(updateContent({} as never, config, {
  ...request, publicationContext: "community", kind: "comment", bodyMarkdown: "invalid",
}), /community|direct Person audience/);
console.log("remote Person Update/Delete canonical audience invariants: OK");
