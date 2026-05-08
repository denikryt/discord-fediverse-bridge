import assert from "node:assert/strict";

import type { GatewayConfig } from "../src/config.js";
import { buildPublishCreateActivity } from "../src/federation-outbound.js";
import { normalizeCreateActivity } from "../src/normalize.js";
import type { PublishContentRequest } from "../src/types.js";

const TEST_ORIGIN = "https://discord-bridge.example.com/";
const TEST_COMMUNITY_URL = "https://lemmy.example/c/hackers";
const TEST_CONFIG: GatewayConfig = {
  actorIdentifier: "bridge",
  actorName: "Bridge",
  actorSummary: "Bridge summary",
  bridgePrivateKeyJwkJson: null,
  bridgePublicKeyJwkJson: null,
  communityActorId: null,
  databaseUrl: "sqlite:///./bridge.db",
  fedifyOrigin: TEST_ORIGIN,
  port: 3000,
  pythonBridgeEventsUrl: "http://127.0.0.1:8080/internal/activitypub/events",
  pythonBridgeSharedSecret: "secret",
  logLevel: "info",
};

async function main(): Promise<void> {
  const postRequest: PublishContentRequest = {
    actorUsername: "alice",
    communityActorUrl: TEST_COMMUNITY_URL,
    kind: "post",
    title: "Bridge post title",
    bodyMarkdown: "hello from discord",
    inReplyToObjectId: null,
  };
  const commentRequest: PublishContentRequest = {
    actorUsername: "alice",
    communityActorUrl: TEST_COMMUNITY_URL,
    kind: "comment",
    title: null,
    bodyMarkdown: "hello comment",
    inReplyToObjectId: `${TEST_ORIGIN}users/alice/post/123`,
  };

  const postEvent = await normalizeCreateActivity(
    buildPublishCreateActivity(TEST_CONFIG, postRequest, TEST_COMMUNITY_URL)
      .activity,
  );
  const commentEvent = await normalizeCreateActivity(
    buildPublishCreateActivity(TEST_CONFIG, commentRequest, TEST_COMMUNITY_URL)
      .activity,
  );

  assert.ok(postEvent);
  assert.ok(commentEvent);
  assert.equal(postEvent.actor_id, `${TEST_ORIGIN}users/alice`);
  assert.equal(postEvent.event_type, "post.created");
  assert.equal(postEvent.community_actor_id, TEST_COMMUNITY_URL);
  assert.equal(commentEvent.actor_id, `${TEST_ORIGIN}users/alice`);
  assert.equal(commentEvent.event_type, "comment.created");
  assert.equal(commentEvent.object.post_ap_id, `${TEST_ORIGIN}users/alice/post/123`);

  console.log("verify:publish-contract passed");
}

await main();
