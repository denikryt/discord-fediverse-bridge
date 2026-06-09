import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import initSqlJs from "./support/sqlite-fixture.js";
import { closeAllPythonBridgeFixtures, startPythonBridgeFixture } from "./support/python-bridge-fixture.js";

import type { GatewayConfig } from "../src/config.js";
import { loadAcceptedRemoteSubscribersByActorUrl } from "../src/python-bridge-client.js";

const TEST_ORIGIN = "https://discord-bridge.example.com/";

async function buildLegacyOnlyConfig(): Promise<GatewayConfig> {
  /** Create a DB that still exposes only the legacy follower table. */
  const sqlJs = await initSqlJs();
  const tempDir = await mkdtemp(path.join(tmpdir(), "fedify-remote-subscriber-naming-"));
  const databasePath = path.join(tempDir, "bridge.db");
  let pythonBridgeInternalUrl = "";
  const db = new sqlJs.Database();
  const legacyRemoteSubscriberTable = ["local", "community", "followers"].join("_");

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
        status VARCHAR(32) NOT NULL
      )
    `);
    db.run(`
      CREATE TABLE ${legacyRemoteSubscriberTable} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        local_community_id INTEGER NOT NULL,
        remote_actor_id VARCHAR(512) NOT NULL,
        remote_inbox_url VARCHAR(512) NOT NULL,
        follow_activity_id VARCHAR(512) NOT NULL,
        status VARCHAR(32) NOT NULL,
        created_at VARCHAR(64)
      )
    `);
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
        10,
        100,
        "hackers",
        "Hackers",
        "A local hackerspace forum.",
        `${TEST_ORIGIN}communities/hackers`,
        `${TEST_ORIGIN}communities/hackers/inbox`,
        `${TEST_ORIGIN}communities/hackers/outbox`,
        `${TEST_ORIGIN}communities/hackers/followers`,
        "public-key",
        "private-key",
        "active",
      ],
    );
    db.run(
      `
        INSERT INTO ${legacyRemoteSubscriberTable} (
          local_community_id,
          remote_actor_id,
          remote_inbox_url,
          follow_activity_id,
          status,
          created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
      `,
      [
        1,
        "https://lemmy.example/u/alice",
        "https://lemmy.example/u/alice/inbox",
        "https://lemmy.example/activities/follow/1",
        "accepted",
        "2026-01-01T00:00:00Z",
      ],
    );
    await writeFile(databasePath, Buffer.from(db.export()));
    pythonBridgeInternalUrl = await startPythonBridgeFixture(databasePath);
  } finally {
    db.close();
  }

  return {
    actorIdentifier: "bridge",
    actorName: "Bridge",
    actorSummary: "Bridge summary",
    pythonBridgeInternalUrl,
    fedifyOrigin: TEST_ORIGIN,
    port: 3000,
        pythonBridgeSharedSecret: "secret",
    logLevel: "info",
  };
}

async function main(): Promise<void> {
  /** Stage 1 should stop silently reading the legacy follower table. */
  const config = await buildLegacyOnlyConfig();

  assert.deepEqual(
    await loadAcceptedRemoteSubscribersByActorUrl(
      config,
      `${TEST_ORIGIN}communities/hackers`,
    ),
    [],
  );

  console.log("verify:remote-subscriber-naming passed");
}

try {
  await main();
} finally {
  await closeAllPythonBridgeFixtures();
}
