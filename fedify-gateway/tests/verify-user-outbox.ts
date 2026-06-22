import assert from "node:assert/strict";
import { createServer } from "node:http";
import type { GatewayConfig } from "../src/config.js";
import { createGatewayApp } from "../src/server.js";
import { buildUserOutboxJson, validateCanonicalUserOutboxUrl } from "../src/user-outbox.js";

const canonical = "https://bridge.example/actors/users/alice/outbox";
const item = { actor_username: "alice", actor_url: "https://bridge.example/actors/users/alice", publication_context: "personal_feed", community_actor_url: null, activity_id: "https://bridge.example/actors/users/alice/activities/create/posts/1", object_id: "https://bridge.example/actors/users/alice/objects/posts/1", kind: "post", title: null, body_markdown: "hello", in_reply_to_object_id: null, published_at: "2026-06-18T00:00:00Z", discord_guild_id: 1, discord_channel_id: 2, discord_message_id: 3 };

async function main(): Promise<void> {
  assert.equal(validateCanonicalUserOutboxUrl(canonical, "https://bridge.example/", "alice"), canonical);
  for (const invalid of ["not a URL", "https://attacker.example/actors/users/alice/outbox", "https://bridge.example/actors/users/bob/outbox", "https://bridge.example/users/alice/outbox", "https://user:pass@bridge.example/actors/users/alice/outbox", "https://bridge.example/actors/users/alice/outbox?x=1", "https://bridge.example/actors/users/alice/outbox#fragment"]) assert.throws(() => validateCanonicalUserOutboxUrl(invalid, "https://bridge.example/", "alice"));
  let outboxUrl: unknown = canonical; let malformedPayload = false; let totalItems = 41;
  const python = createServer((request, response) => {
    response.setHeader("Content-Type", "application/json");
    if (request.url === "/internal/fedify/actors/users/alice") {
      response.end(JSON.stringify({
        activitypub_username: "alice", actor_url: "https://bridge.example/actors/users/alice",
        inbox_url: "https://bridge.example/actors/users/alice/inbox", outbox_url: canonical,
        followers_url: "https://bridge.example/actors/users/alice/followers",
        public_key_pem: "public", private_key_pem: "private", display_name: "Alice",
        summary: null, fields: [], avatar: null, header: null,
      }));
      return;
    }
    if (request.url?.startsWith("/internal/fedify/actors/users/alice/outbox")) {
      if (malformedPayload) { response.end(JSON.stringify({ outbox_url: canonical, total_items: "bad", page: 1, page_size: 40, items: [] })); return; }
      response.end(JSON.stringify({ outbox_url: outboxUrl, total_items: totalItems, page: 1, page_size: 40, items: totalItems > 0 ? [item] : [] })); return;
    }
    response.statusCode = 404; response.end(JSON.stringify({ detail: "not found" }));
  });
  await new Promise<void>((resolve) => python.listen(0, "127.0.0.1", resolve));
  const address = python.address(); if (address == null || typeof address === "string") throw new Error("missing fixture address");
  const config: GatewayConfig = { actorIdentifier: "bridge", actorName: "Bridge", actorSummary: "Bridge", fedifyOrigin: "https://bridge.example/", port: 3000, pythonBridgeInternalUrl: `http://127.0.0.1:${address.port}`, pythonBridgeSharedSecret: "secret", logLevel: "info" };
  const app = createGatewayApp(config);
  try {
    for (const route of ["/actors/users/alice/outbox"]) {
      const response = await app.request(route); assert.equal(response.status, 200); const body = await response.json() as Record<string, unknown>; assert.equal(body.id, canonical); assert.equal(body.first, `${canonical}?page=1`);
      const page = await app.request(`${route}?page=1`); assert.equal(page.status, 200); const pageBody = await page.json() as Record<string, unknown>; assert.equal(pageBody.id, `${canonical}?page=1`); assert.equal(pageBody.partOf, canonical);
    }
    totalItems = 0; const empty = await app.request("/actors/users/alice/outbox"); const emptyBody = await empty.json() as Record<string, unknown>; assert.equal(emptyBody.totalItems, 0); assert.equal(emptyBody.first, undefined); const emptyPage = await app.request("/actors/users/alice/outbox?page=1"); assert.equal(emptyPage.status, 200); assert.deepEqual((await emptyPage.json() as Record<string, unknown>).orderedItems, []);
    for (const invalid of ["https://attacker.example/actors/users/alice/outbox", "not a URL", "https://bridge.example/actors/users/bob/outbox", "https://bridge.example/users/alice/outbox"]) { outboxUrl = invalid; for (const route of ["/actors/users/alice/outbox"]) assert.equal((await app.request(route)).status, 502); }
    outboxUrl = canonical; malformedPayload = true; assert.equal((await app.request("/actors/users/alice/outbox")).status, 502);
  } finally { await new Promise<void>((resolve, reject) => python.close((error) => error == null ? resolve() : reject(error))); }
  const row = { outboxUrl: canonical, totalItems: 0, page: 1, pageSize: 40, items: [] }; assert.equal("first" in buildUserOutboxJson(row, null), false);
  console.log("user outbox HTTP and identity verification passed");
}
await main();
