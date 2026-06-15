import assert from "node:assert/strict";
import { createServer } from "node:http";

import type { GatewayConfig } from "../src/config.js";
import { createGatewayApp } from "../src/server.js";

/** Exercise the public User Actor followers collection through both supported routes. */
async function main(): Promise<void> {
  const requests: string[] = [];
  const followers = Array.from({ length: 81 }, (_, index) =>
    `https://remote.example/users/follower-${81 - index}`,
  );
  let failFollowersRead = false;

  const python = createServer((request, response) => {
    requests.push(request.url ?? "");
    response.setHeader("Content-Type", "application/json");
    if (request.url === "/internal/fedify/actors/users/alice") {
      response.end(JSON.stringify({
        activitypub_username: "alice",
        actor_url: "https://bridge.example/actors/alice",
        inbox_url: "https://bridge.example/actors/alice/inbox",
        outbox_url: "https://bridge.example/actors/alice/outbox",
        followers_url: "https://bridge.example/actors/alice/followers",
        public_key_pem: "public",
        private_key_pem: "private",
        display_name: "Alice",
        summary: null,
        fields: [],
        avatar: null,
        header: null,
      }));
      return;
    }
    if (request.url?.startsWith("/internal/fedify/actors/users/alice/followers")) {
      if (failFollowersRead) {
        response.statusCode = 500;
        response.end(JSON.stringify({ detail: "database unavailable" }));
        return;
      }
      const url = new URL(request.url, "http://python.internal");
      const page = Number(url.searchParams.get("page") ?? "1");
      const pageSize = Number(url.searchParams.get("page_size") ?? "40");
      const start = (page - 1) * pageSize;
      response.end(JSON.stringify({
        followers_url: "https://bridge.example/actors/alice/followers",
        total_items: followers.length,
        page,
        page_size: pageSize,
        actor_ids: followers.slice(start, start + pageSize),
      }));
      return;
    }
    response.statusCode = 404;
    response.end(JSON.stringify({ detail: "not found" }));
  });
  await new Promise<void>((resolve) => python.listen(0, "127.0.0.1", resolve));
  const address = python.address();
  if (address == null || typeof address === "string") throw new Error("missing fixture address");

  const config: GatewayConfig = {
    actorIdentifier: "bridge",
    actorName: "Bridge",
    actorSummary: "Bridge summary",
    fedifyOrigin: "https://bridge.example/",
    port: 3000,
    pythonBridgeInternalUrl: `http://127.0.0.1:${address.port}`,
    pythonBridgeSharedSecret: "secret",
    logLevel: "info",
  };
  const app = createGatewayApp(config);

  try {
    const top = await app.request("/actors/alice/followers");
    assert.equal(top.status, 200);
    const topBody = await top.json() as Record<string, unknown>;
    assert.equal(topBody.type, "OrderedCollection");
    assert.equal(topBody.totalItems, 81);
    assert.equal(topBody.first, "https://bridge.example/actors/alice/followers?page=true");

    const alias = await app.request("/users/alice/followers");
    assert.equal(alias.status, 200);
    assert.deepEqual(await alias.json(), topBody);

    const firstPage = await app.request("/actors/alice/followers?page=true");
    assert.equal(firstPage.status, 200);
    const firstBody = await firstPage.json() as Record<string, unknown>;
    assert.equal(firstBody.id, "https://bridge.example/actors/alice/followers?page=true");
    assert.equal(firstBody.partOf, "https://bridge.example/actors/alice/followers");
    assert.equal((firstBody.orderedItems as unknown[]).length, 40);
    assert.equal((firstBody.orderedItems as string[])[0], followers[0]);
    assert.equal(firstBody.prev, undefined);
    assert.equal(firstBody.next, "https://bridge.example/actors/alice/followers?page=true&page_number=2");

    const secondPage = await app.request("/actors/alice/followers?page=true&page_number=2&page_size=80");
    assert.equal(secondPage.status, 200);
    const secondBody = await secondPage.json() as Record<string, unknown>;
    assert.equal((secondBody.orderedItems as unknown[]).length, 40);
    assert.equal(secondBody.prev, "https://bridge.example/actors/alice/followers?page=true&page_number=1");
    assert.equal(secondBody.next, "https://bridge.example/actors/alice/followers?page=true&page_number=3");
    assert.ok(requests.some((path) => path.endsWith("/followers?page=2&page_size=40")));

    assert.equal((await app.request("/actors/alice/followers?page=false")).status, 400);
    assert.equal((await app.request("/actors/alice/followers?page=true&page_number=0")).status, 400);
    assert.equal((await app.request("/actors/alice/followers?page=true&page_number=abc")).status, 400);
    assert.equal((await app.request("/actors/alice/followers?page=true&page_number=4")).status, 404);

    followers.splice(0, followers.length);
    const empty = await app.request("/actors/alice/followers");
    const emptyBody = await empty.json() as Record<string, unknown>;
    assert.equal(emptyBody.totalItems, 0);
    assert.equal(emptyBody.first, undefined);

    failFollowersRead = true;
    const failed = await app.request("/actors/alice/followers");
    assert.equal(failed.status, 502);
    const failedBody = await failed.json() as Record<string, unknown>;
    assert.notEqual(failedBody.totalItems, 0);
  } finally {
    await new Promise<void>((resolve, reject) => python.close((error) => error == null ? resolve() : reject(error)));
  }

  console.log("verify:user-actor-followers-collection passed");
}

await main();
