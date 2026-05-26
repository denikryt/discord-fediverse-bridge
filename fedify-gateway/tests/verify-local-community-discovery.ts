/**
 * Gateway verification for public local-community discovery.
 *
 * The bridge-specific discovery endpoint is the contract used by Python
 * command discovery for same-instance and remote bridge community lookup.
 * This test locks the public JSON shape and ensures the endpoint exposes only
 * actor identity fields, not internal Discord routing state.
 */
import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import initSqlJs from "sql.js";

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
  const config = await buildConfig();
  const app = createGatewayApp(config);

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
}

async function buildConfig(): Promise<GatewayConfig> {
  /** Build a temporary Python-owned database with public local communities. */
  const sqlJs = await initSqlJs();
  const tempDir = await mkdtemp(path.join(tmpdir(), "fedify-local-community-discovery-"));
  const databasePath = path.join(tempDir, "bridge.db");
  const db = new sqlJs.Database();

  try {
    db.run(`
      CREATE TABLE local_communities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        discord_guild_id INTEGER NOT NULL,
        discord_forum_channel_id INTEGER NOT NULL,
        slug VARCHAR(255) NOT NULL,
        display_name VARCHAR(255) NOT NULL,
        summary TEXT NOT NULL,
        actor_url VARCHAR(512) NOT NULL,
        inbox_url VARCHAR(512) NOT NULL,
        outbox_url VARCHAR(512) NOT NULL,
        followers_url VARCHAR(512) NOT NULL,
        public_key_pem TEXT NOT NULL,
        private_key_pem TEXT NOT NULL,
        status VARCHAR(32) NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
      )
    `);
    insertCommunity(db, {
      guildId: 10,
      forumId: 100,
      slug: "hackers",
      title: "Hackers",
      summary: "A local hackerspace forum.",
    });
    insertCommunity(db, {
      guildId: 20,
      forumId: 200,
      slug: "announcements",
      title: "Announcements",
      summary: "One-way service updates.",
    });
    await writeFile(databasePath, Buffer.from(db.export()));
  } finally {
    db.close();
  }

  return {
    actorIdentifier: "bridge",
    actorName: "Discord Bridge",
    actorSummary: "Test gateway",
    bridgePrivateKeyJwkJson: null,
    bridgePublicKeyJwkJson: null,
    databaseUrl: `sqlite:///${databasePath}`,
    fedifyOrigin: TEST_ORIGIN,
    port: 3000,
    pythonBridgeEventsUrl: "http://127.0.0.1:8081/internal/activitypub/events",
    pythonBridgeSharedSecret: "secret",
    logLevel: "info",
  };
}

function insertCommunity(
  db: initSqlJs.Database,
  args: { guildId: number; forumId: number; slug: string; title: string; summary: string },
): void {
  /** Seed one local community row with the public fields discovery must expose. */
  db.run(
    `
      INSERT INTO local_communities (
        discord_guild_id,
        discord_forum_channel_id,
        slug,
        display_name,
        summary,
        actor_url,
        inbox_url,
        outbox_url,
        followers_url,
        public_key_pem,
        private_key_pem,
        status
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `,
    [
      args.guildId,
      args.forumId,
      args.slug,
      args.title,
      args.summary,
      `${TEST_ORIGIN}communities/${args.slug}`,
      `${TEST_ORIGIN}communities/${args.slug}/inbox`,
      `${TEST_ORIGIN}communities/${args.slug}/outbox`,
      `${TEST_ORIGIN}communities/${args.slug}/followers`,
      "public",
      "private",
      "active",
    ],
  );
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
