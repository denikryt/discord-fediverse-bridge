/**
 * Regression checks for direct Create delivery targeting typed local community URLs.
 *
 * Local communities use `/actors/communities/{slug}` as the canonical actor id.
 * The normalizer must treat that typed path as a valid community actor target so
 * direct ActivityPub Create(Page) deliveries reach the Python bridge.
 */

import assert from "node:assert/strict";
import { Create } from "@fedify/vocab";
import type { PythonBridgeReadClient } from "../src/python-bridge-client.js";

import { normalizeCreateActivity, normalizeCreateActivityFromJson } from "../src/normalize.js";

const COMMUNITY_URL = "https://bridge.example/actors/communities/hackers";

async function main(): Promise<void> {
  await testDirectCreateToTypedLocalCommunityPathNormalizesPost();
  await testSignedBridgePersonOpaquePageUsesNonLemmySentinel();
  await testUnrelatedOpaquePageStillRequiresLemmyNumericId();
  await testResolvedActorRemainsIdentifiableInVerifiedCreate();
  await testCommunityReplyParentContract();
  console.log("verify:normalize-local-community-create passed");
}

/** Real Fedify normalization distinguishes a root reply from a nested reply. */
async function testCommunityReplyParentContract(): Promise<void> {
  const actor = "https://remote.example/actors/users/alice";
  const root = "https://bridge.example/actors/users/local/objects/posts/1";
  const parent = "https://bridge.example/actors/users/local/objects/comments/1";
  // Only the outer Python HTTP reads are replaced; normalize real Fedify Notes.
  const client = {
    loadConversationTargetByObjectId: async () => null,
    loadPublishedActivityObjectByObjectId: async (id: string) => ({
      kind: id === root ? "post" : "comment", objectId: id,
      inReplyToObjectId: id === root ? null : root,
    }),
  } as unknown as PythonBridgeReadClient;
  for (const target of [root, parent]) {
    const activity = await Create.fromJsonLd({
      "@context": "https://www.w3.org/ns/activitystreams", type: "Create",
      id: `${actor}/activities/create/comments/1`,
      actor: { type: "Person", id: actor, preferredUsername: "alice" },
      object: { type: "Note", id: `${actor}/objects/comments/1`,
        attributedTo: actor, audience: COMMUNITY_URL, inReplyTo: target,
        published: "2026-11-12T00:00:00Z", content: "What's up!" },
    });
    const event = await normalizeCreateActivity(activity, { pythonBridgeClient: client });
    assert.equal(event?.object.post_ap_id, root);
    assert.equal(event?.object.parent_ap_id, target === root ? null : parent);
  }
}

/** A resolved Person stays identifiable in Fedify's serialized Create. */
async function testResolvedActorRemainsIdentifiableInVerifiedCreate(): Promise<void> {
  const actor = "https://remote.example/actors/users/alice";
  const objectId = `${actor}/objects/posts/opaque-token`;
  const activity = await Create.fromJsonLd({
    "@context": "https://www.w3.org/ns/activitystreams",
    type: "Create", id: `${actor}/activities/create/posts/opaque-token`,
    actor: { type: "Person", id: actor, preferredUsername: "alice" },
    object: {
      type: "Page", id: objectId, attributedTo: actor,
      audience: COMMUNITY_URL, to: [COMMUNITY_URL],
      published: "2026-11-12T00:00:00Z", name: "Bridge topic",
    },
  });
  const event = await normalizeCreateActivity(activity);
  const serialized = await activity.toJsonLd() as Record<string, unknown>;
  const serializedActor = serialized.actor as { id?: string };
  assert.equal(event?.actor_id, actor);
  assert.equal(serializedActor.id, actor);
}

/**
 * Action: a remote instance sends a direct Create(Page) to one local community
 * actor whose canonical id lives under `/actors/communities/{slug}`.
 *
 * Expected: normalization succeeds and preserves the canonical community actor
 * URL instead of dropping the event as a non-community target.
 */
async function testDirectCreateToTypedLocalCommunityPathNormalizesPost(): Promise<void> {
  const event = await normalizeCreateActivityFromJson({
    type: "Create",
    id: "https://lemmy.example/activities/create/post/1",
    actor: "https://lemmy.example/u/bob",
    to: ["as:Public", COMMUNITY_URL],
    object: {
      type: "Page",
      id: "https://lemmy.example/post/1",
      attributedTo: "https://lemmy.example/u/bob",
      audience: COMMUNITY_URL,
      to: ["as:Public", COMMUNITY_URL],
      cc: ["https://lemmy.example/u/bob"],
      name: "Remote topic",
      content: "<p>hello from lemmy</p>",
      source: {
        content: "hello from lemmy",
        mediaType: "text/markdown",
      },
      published: "2026-05-19T10:00:00Z",
      url: "https://lemmy.example/post/1",
    },
  });

  assert.ok(event, "The direct Create(Page) must normalize for local communities");
  assert.equal(event?.event_type, "post.created");
  assert.equal(event?.community_actor_id, COMMUNITY_URL);
  assert.equal(event?.object.lemmy_id, 1);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

/** A registered Bridge Person may use a stable opaque post token. */
async function testSignedBridgePersonOpaquePageUsesNonLemmySentinel(): Promise<void> {
  const actor = "https://remote.example/actors/users/alice";
  const postId = `${actor}/objects/posts/opaque-digest-31a`;
  const event = await normalizeCreateActivityFromJson({
    type: "Create", id: `${actor}/activities/create/posts/opaque-digest-31a`,
    actor, to: [COMMUNITY_URL],
    object: {
      type: "Page", id: postId, attributedTo: actor,
      name: "Bridge topic", audience: COMMUNITY_URL, to: [COMMUNITY_URL],
      source: { content: "Bridge body", mediaType: "text/markdown" },
      published: "2026-11-12T00:00:00Z", url: postId,
    },
  });
  assert.ok(event);
  assert.equal(event.event_type, "post.created");
  assert.equal(event.actor_id, actor);
  assert.equal(event.object.ap_id, postId);
  assert.equal(event.object.lemmy_id, 0);
}

/** An opaque arbitrary Page cannot silently weaken Lemmy's numeric contract. */
async function testUnrelatedOpaquePageStillRequiresLemmyNumericId(): Promise<void> {
  const actor = "https://remote.example/actors/users/alice";
  await assert.rejects(() => normalizeCreateActivityFromJson({
    type: "Create", id: `${actor}/activities/create/posts/unrelated`, actor,
    object: {
      type: "Page", id: "https://remote.example/unrelated/opaque",
      attributedTo: actor, audience: COMMUNITY_URL, to: [COMMUNITY_URL],
      published: "2026-11-12T00:00:00Z", name: "Untrusted opaque post",
    },
  }), /Could not parse Lemmy post numeric id/);
}
