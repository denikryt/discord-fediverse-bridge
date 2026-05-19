/**
 * Regression checks for direct Create delivery targeting local community URLs.
 *
 * Local communities use `/communities/{slug}` as the canonical actor id. The
 * normalizer must treat that path as a valid community actor target so direct
 * Lemmy Create(Page) deliveries reach the Python bridge.
 */

import assert from "node:assert/strict";

import { normalizeCreateActivityFromJson } from "../src/normalize.js";

const COMMUNITY_URL = "https://bridge.example/communities/hackers";

async function main(): Promise<void> {
  await testDirectCreateToLocalCommunityPathNormalizesPost();
  console.log("verify:normalize-local-community-create passed");
}

/**
 * Action: a remote instance sends a direct Create(Page) to one local community
 * actor whose canonical id lives under `/communities/{slug}`.
 *
 * Expected: normalization succeeds and preserves the canonical community actor
 * URL instead of dropping the event as a non-community target.
 */
async function testDirectCreateToLocalCommunityPathNormalizesPost(): Promise<void> {
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
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
