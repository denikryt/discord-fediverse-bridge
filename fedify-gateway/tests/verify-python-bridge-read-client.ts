import assert from "node:assert/strict";
import { createServer } from "node:http";

import { PythonBridgeClient } from "../src/python-bridge-client.js";

const secret = "test-secret";
const seen: Array<{ path: string; authorization: string | undefined; body: string }> = [];
const server = createServer(async (request, response) => {
  let body = "";
  for await (const chunk of request) body += chunk;
  seen.push({ path: request.url ?? "", authorization: request.headers.authorization, body });
  response.setHeader("Content-Type", "application/json");
  if (request.headers.authorization !== `Bearer ${secret}`) {
    response.statusCode = 401;
    response.end(JSON.stringify({ detail: "unauthorized" }));
    return;
  }
  const path = request.url ?? "";
  if (path.endsWith("/missing")) {
    response.statusCode = 404;
    response.end(JSON.stringify({ detail: "missing" }));
    return;
  }
  if (path === "/internal/fedify/actors/bridge/key") {
    response.end(JSON.stringify({ actor_url: "https://bridge.example/actors/bridge", key_id: "https://bridge.example/actors/bridge#main-key", key_format: "jwk", algorithm: "RSASSA-PKCS1-v1_5", public_key_data: "{}", private_key_data: "{}" }));
    return;
  }
  if (path.startsWith("/internal/fedify/actors/users/")) {
    response.end(JSON.stringify({ activitypub_username: "alice", actor_url: "https://bridge.example/users/alice", inbox_url: "https://bridge.example/users/alice/inbox", outbox_url: "https://bridge.example/users/alice/outbox", followers_url: "https://bridge.example/users/alice/followers", public_key_pem: "public", private_key_pem: "private" }));
    return;
  }
  if (path.startsWith("/internal/fedify/actors/communities/")) {
    response.end(JSON.stringify({ slug: "test", actor_url: "https://bridge.example/communities/test", inbox_url: "https://bridge.example/communities/test/inbox", outbox_url: "https://bridge.example/communities/test/outbox", followers_url: "https://bridge.example/communities/test/followers", display_name: "Test", summary: null, public_key_pem: "public", private_key_pem: "private" }));
    return;
  }
  if (path === "/internal/fedify/communities") {
    response.end(JSON.stringify({ items: [{ id: 1, slug: "test", display_name: "Test", summary: null, actor_url: "https://bridge.example/communities/test" }] }));
    return;
  }
  if (path === "/internal/fedify/communities/subscribers") {
    response.end(JSON.stringify({ items: [{ remote_actor_id: "https://remote.example/u/a", remote_inbox_url: "https://remote.example/inbox", follow_activity_id: "https://remote.example/f/1", status: "accepted" }] }));
    return;
  }
  if (path === "/internal/fedify/published-objects/resolve") {
    response.end(JSON.stringify({ actor_username: "alice", actor_url: "https://bridge.example/users/alice", community_actor_url: "https://bridge.example/communities/test", activity_id: "https://bridge.example/activities/1", object_id: "https://bridge.example/objects/1", kind: "post", title: "Title", body_markdown: "Body", in_reply_to_object_id: null, published_at: "2026-01-01T00:00:00+00:00", discord_channel_id: 1, discord_message_id: 2 }));
    return;
  }
  if (path === "/internal/fedify/message-mappings/resolve") {
    response.end(JSON.stringify({ source_platform: "discord", source_id: "2", activity_id: "https://bridge.example/activities/1", object_id: "https://bridge.example/objects/1", actor_url: "https://bridge.example/users/alice", community_actor_url: "https://bridge.example/communities/test", discord_channel_id: 1, discord_message_id: 2 }));
    return;
  }
  if (path === "/internal/fedify/channel-community-subscriptions") {
    response.end(JSON.stringify({ items: [{ community_actor_url: "https://remote.example/c/test", follow_activity_id: "https://bridge.example/follows/1", status: "accepted" }] }));
    return;
  }
  if (path === "/internal/activitypub/events") {
    response.end(JSON.stringify({ status: "processed" }));
    return;
  }
  response.statusCode = 500;
  response.end(JSON.stringify({ detail: "unexpected" }));
});
await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
const address = server.address();
if (address == null || typeof address === "string") throw new Error("missing test address");
const client = new PythonBridgeClient(`http://127.0.0.1:${address.port}`, secret);
try {
  assert.equal((await client.loadBridgeActorKey()).keyFormat, "jwk");
  assert.equal(await client.loadRegisteredUserByUsername("missing"), null);
  assert.equal((await client.loadRegisteredUserByUsername("alice"))?.activitypubUsername, "alice");
  assert.equal((await client.loadLocalCommunityBySlug("test"))?.slug, "test");
  assert.equal((await client.loadLocalCommunityByActorUrl("https://bridge.example/communities/test"))?.slug, "test");
  assert.equal((await client.listLocalCommunities("https://bridge.example"))[0]?.handle, "!test@bridge.example");
  assert.equal((await client.loadAcceptedRemoteSubscribersByActorUrl("https://bridge.example/communities/test"))[0]?.status, "accepted");
  assert.equal((await client.loadPublishedActivityObjectByObjectId("https://bridge.example/objects/1"))?.discordMessageId, 2);
  assert.equal((await client.loadPublishedActivityObjectByActivityId("https://bridge.example/activities/1"))?.objectId, "https://bridge.example/objects/1");
  assert.equal((await client.loadMessageMappingByObjectId("https://bridge.example/objects/1"))?.sourcePlatform, "discord");
  assert.equal((await client.listChannelCommunitySubscriptions())[0]?.status, "accepted");
  await client.deliverEvent({ actor_id: "a", community_actor_id: "c", delivery_id: "d", event_type: "follow.accepted", object: { follow_activity_id: "f" }, occurred_at: "2026-01-01T00:00:00Z" });
  assert.ok(seen.every((entry) => entry.authorization === `Bearer ${secret}`));
  assert.ok(seen.some((entry) => entry.path === "/internal/fedify/actors/communities/resolve" && entry.body.includes("actor_url")));
  assert.ok(seen.some((entry) => entry.path === "/internal/activitypub/events"));
} finally {
  server.close();
}
console.log("python bridge read client verification passed");
