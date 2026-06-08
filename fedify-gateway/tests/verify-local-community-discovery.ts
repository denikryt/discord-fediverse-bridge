/**
 * Gateway verification for public local-community discovery.
 *
 * The bridge-specific discovery endpoint is the contract used by Python
 * command discovery for same-instance and remote bridge community lookup.
 * This test locks the public JSON shape and ensures the endpoint exposes only
 * actor identity fields, not internal Discord routing state.
 */
import assert from "node:assert/strict";
import { createServer } from "node:http";

import { createGatewayApp } from "../src/server.js";
import type { GatewayConfig } from "../src/config.js";

const TEST_ORIGIN = "https://discord-bridge.example.com/";

async function main(): Promise<void> {
  await testDiscoveryEndpointReturnsStablePublicCommunitySummaries();
  console.log("verify:local-community-discovery passed");
}

/**
 * Action: fetch the bridge discovery endpoint with two local communities.
 * Expected: the response lists sorted public identity fields only.
 */
async function testDiscoveryEndpointReturnsStablePublicCommunitySummaries(): Promise<void> {
  const fixture = await buildConfig();
  const app = createGatewayApp(fixture.config);

  const response = await app.request("/.well-known/discord-fediverse-bridge/communities");
  assert.equal(response.status, 200);

  const payload = await response.json() as {
    software: string;
    communities: Array<Record<string, unknown>>;
  };
  assert.equal(payload.software, "discord-fediverse-bridge");
  assert.equal(payload.communities.length, 2);
  assert.equal(payload.communities[0]?.slug, "announcements");
  assert.equal(payload.communities[1]?.slug, "hackers");
  assert.equal(
    payload.communities[0]?.actor_id,
    `${TEST_ORIGIN}communities/announcements`,
  );
  assert.equal(
    payload.communities[0]?.alternate_actor_id,
    `${TEST_ORIGIN}c/announcements`,
  );
  assert.equal(
    payload.communities[0]?.handle,
    "!announcements@discord-bridge.example.com",
  );
  assert.equal(payload.communities[0]?.discord_forum_channel_id, undefined);
  assert.equal(payload.communities[0]?.discord_guild_id, undefined);
  await fixture.close();
}

async function buildConfig(): Promise<{ config: GatewayConfig; close: () => Promise<void> }> {
  /** Expose the Python bridge discovery read model over the authenticated boundary. */
  const server = createServer((request, response) => {
    if (request.url !== "/internal/fedify/communities") {
      response.statusCode = 404;
      response.end(JSON.stringify({ detail: "not found" }));
      return;
    }
    response.setHeader("Content-Type", "application/json");
    response.end(JSON.stringify({ items: [
      { id: 2, slug: "announcements", display_name: "Announcements", summary: "One-way service updates.", actor_url: `${TEST_ORIGIN}communities/announcements` },
      { id: 1, slug: "hackers", display_name: "Hackers", summary: "A local hackerspace forum.", actor_url: `${TEST_ORIGIN}communities/hackers` },
    ] }));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  server.unref();
  const address = server.address();
  if (address == null || typeof address === "string") throw new Error("missing bridge fixture address");

  return { config: {
    actorIdentifier: "bridge",
    actorName: "Discord Bridge",
    actorSummary: "Test gateway",
    pythonBridgeInternalUrl: `http://127.0.0.1:${address.port}`,
    fedifyOrigin: TEST_ORIGIN,
    port: 3000,
    pythonBridgeSharedSecret: "secret",
    logLevel: "info",
  }, close: async () => {
    await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  } };
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
