/**
 * Verification tests for typed local ActivityPub identity URL helpers.
 *
 * These assertions define the typed actor grammar target grammar only. Production gateway
 * routes, WebFinger output, actor-store mapping, and outbound emitters remain on
 * their pre-cutover paths until later work switch them atomically.
 */

import assert from "node:assert/strict";

import {
  buildActorAddress,
  buildCommunityAcceptFollowActivityId,
  buildCommunityAnnounceCommentActivityId,
  buildCommunityAnnouncePostActivityId,
  buildCommunityCommentObjectId,
  buildCommunityCreateCommentActivityId,
  buildCommunityCreatePostActivityId,
  buildCommunityDeleteCommentActivityId,
  buildCommunityDeletePostActivityId,
  buildCommunityPostObjectId,
  buildCommunityUpdateCommentActivityId,
  buildCommunityUpdatePostActivityId,
  buildServiceFollowActivityId,
  buildServiceUndoFollowActivityId,
  buildUserAcceptFollowActivityId,
  buildUserCommentObjectId,
  buildUserCreateCommentActivityId,
  buildUserCreatePostActivityId,
  buildUserDeleteCommentActivityId,
  buildUserDeletePostActivityId,
  buildUserPostObjectId,
  buildUserProfileUpdateActivityId,
  buildUserUpdateCommentActivityId,
  buildUserUpdatePostActivityId,
  parseLocalActorUrl,
} from "../src/activitypub-identity-urls.js";

function assertThrowsForOrigin(origin: string): void {
  /** Ensure non-origin values cannot become durable AP identity roots. */
  assert.throws(() => buildActorAddress(origin, "user", "alice"));
}

const user = buildActorAddress("https://bridge.example", "user", "alice");
assert.equal(user.actor.href, "https://bridge.example/actors/users/alice");
assert.equal(user.inbox.href, "https://bridge.example/actors/users/alice/inbox");
assert.equal(user.outbox.href, "https://bridge.example/actors/users/alice/outbox");
assert.equal(user.followers.href, "https://bridge.example/actors/users/alice/followers");
assert.equal(user.publicKey.href, "https://bridge.example/actors/users/alice#main-key");

const community = buildActorAddress(new URL("https://bridge.example/"), "community", "python");
assert.equal(community.followers.href, "https://bridge.example/actors/communities/python/followers");

const service = buildActorAddress("https://bridge.example", "service", "bridge");
assert.equal(service.publicKey.href, "https://bridge.example/actors/services/bridge#main-key");
assert.equal(
  buildActorAddress("https://bridge.example:8443/", "user", "alice").actor.href,
  "https://bridge.example:8443/actors/users/alice",
);

for (const origin of [
  "https://bridge.example/base",
  "https://bridge.example/app/",
  "https://bridge.example?x=1",
  "https://bridge.example#x",
  "https://user:pass@bridge.example",
  "/bridge",
]) {
  assertThrowsForOrigin(origin);
}

const parsedUser = parseLocalActorUrl(
  "https://bridge.example",
  "https://bridge.example/actors/users/python",
);
const parsedCommunity = parseLocalActorUrl(
  "https://bridge.example",
  "https://bridge.example/actors/communities/python",
);
assert.equal(parsedUser?.kind, "user");
assert.equal(parsedUser?.identifier, "python");
assert.equal(parsedCommunity?.kind, "community");
assert.equal(parsedCommunity?.identifier, "python");
assert.notEqual(parsedUser?.actor.href, parsedCommunity?.actor.href);

for (const actorUrl of [
  "https://bridge.example/actors/alice",
  "https://bridge.example/communities/python",
  "https://bridge.example/users/alice",
  "https://bridge.example/c/python",
  "https://bridge.example/actors/user/alice",
  "https://bridge.example/actors/users/alice/inbox",
  "https://bridge.example/actors/services/",
  "https://bridge.example/actors/users/alice?x=1",
  "https://bridge.example/actors/users/alice#main-key",
  "https://bridge.example/actors/users/alice%2Fbob",
  "https://user:pass@bridge.example/actors/users/alice",
  "https://remote.example/actors/users/alice",
]) {
  assert.equal(parseLocalActorUrl("https://bridge.example", actorUrl), null);
}

assert.equal(
  buildUserPostObjectId("https://bridge.example", "alice", "id with space").href,
  "https://bridge.example/actors/users/alice/objects/posts/id%20with%20space",
);

const grammarCases: Array<[URL, string]> = [
  [buildUserPostObjectId("https://bridge.example", "alice", "post1"), "https://bridge.example/actors/users/alice/objects/posts/post1"],
  [buildUserCommentObjectId("https://bridge.example", "alice", "comment1"), "https://bridge.example/actors/users/alice/objects/comments/comment1"],
  [buildUserCreatePostActivityId("https://bridge.example", "alice", "a1"), "https://bridge.example/actors/users/alice/activities/create/posts/a1"],
  [buildUserCreateCommentActivityId("https://bridge.example", "alice", "a2"), "https://bridge.example/actors/users/alice/activities/create/comments/a2"],
  [buildUserUpdatePostActivityId("https://bridge.example", "alice", "a3"), "https://bridge.example/actors/users/alice/activities/update/posts/a3"],
  [buildUserUpdateCommentActivityId("https://bridge.example", "alice", "a4"), "https://bridge.example/actors/users/alice/activities/update/comments/a4"],
  [buildUserDeletePostActivityId("https://bridge.example", "alice", "a5"), "https://bridge.example/actors/users/alice/activities/delete/posts/a5"],
  [buildUserDeleteCommentActivityId("https://bridge.example", "alice", "a6"), "https://bridge.example/actors/users/alice/activities/delete/comments/a6"],
  [buildUserAcceptFollowActivityId("https://bridge.example", "alice", "f1"), "https://bridge.example/actors/users/alice/activities/accept/follows/f1"],
  [buildUserProfileUpdateActivityId("https://bridge.example", "alice", 7), "https://bridge.example/actors/users/alice/activities/update/profile/7"],
  [buildCommunityPostObjectId("https://bridge.example", "python", "post1"), "https://bridge.example/actors/communities/python/objects/posts/post1"],
  [buildCommunityCommentObjectId("https://bridge.example", "python", "comment1"), "https://bridge.example/actors/communities/python/objects/comments/comment1"],
  [buildCommunityCreatePostActivityId("https://bridge.example", "python", "a1"), "https://bridge.example/actors/communities/python/activities/create/posts/a1"],
  [buildCommunityCreateCommentActivityId("https://bridge.example", "python", "a2"), "https://bridge.example/actors/communities/python/activities/create/comments/a2"],
  [buildCommunityAnnouncePostActivityId("https://bridge.example", "python", "a3"), "https://bridge.example/actors/communities/python/activities/announce/posts/a3"],
  [buildCommunityAnnounceCommentActivityId("https://bridge.example", "python", "a4"), "https://bridge.example/actors/communities/python/activities/announce/comments/a4"],
  [buildCommunityUpdatePostActivityId("https://bridge.example", "python", "a5"), "https://bridge.example/actors/communities/python/activities/update/posts/a5"],
  [buildCommunityUpdateCommentActivityId("https://bridge.example", "python", "a6"), "https://bridge.example/actors/communities/python/activities/update/comments/a6"],
  [buildCommunityDeletePostActivityId("https://bridge.example", "python", "a7"), "https://bridge.example/actors/communities/python/activities/delete/posts/a7"],
  [buildCommunityDeleteCommentActivityId("https://bridge.example", "python", "a8"), "https://bridge.example/actors/communities/python/activities/delete/comments/a8"],
  [buildCommunityAcceptFollowActivityId("https://bridge.example", "python", "f1"), "https://bridge.example/actors/communities/python/activities/accept/follows/f1"],
  [buildServiceFollowActivityId("https://bridge.example", "bridge", "f1"), "https://bridge.example/actors/services/bridge/activities/follow/f1"],
  [buildServiceUndoFollowActivityId("https://bridge.example", "bridge", "u1"), "https://bridge.example/actors/services/bridge/activities/undo/follows/u1"],
];
for (const [actual, expected] of grammarCases) {
  assert.equal(actual.href, expected);
}

assert.throws(() => buildActorAddress("https://bridge.example", "user", "Alice"));
assert.throws(() => buildActorAddress("https://bridge.example", "community", "bad/slug"));
assert.throws(() => buildActorAddress("https://bridge.example", "service", "bad/bridge"));
assert.throws(() => buildActorAddress("https://bridge.example", "service", ""));

console.log("verify-activitypub-identity-urls passed");
