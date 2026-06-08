import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { Follow } from "@fedify/vocab";
import { exportJwk, generateCryptoKeyPair } from "@fedify/fedify";
import initSqlJs, { seedBridgeActorJwk } from "./support/sqlite-fixture.js";
import { startPythonBridgeFixture } from "./support/python-bridge-fixture.js";

import { buildLocalFollowRequestedEvent } from "../src/federation.js";
import type { GatewayConfig } from "../src/config.js";

const TEST_ORIGIN = "https://discord-bridge.example.com/";
const REMOTE_ORIGIN = "https://lemmy.example/";

async function main(): Promise<void> {
  const sqlJs = await initSqlJs();
  const tempDir = await mkdtemp(path.join(tmpdir(), "fedify-local-follow-"));
  const databasePath = path.join(tempDir, "bridge.db");
  let pythonBridgeInternalUrl = "";
  const bridgeKeys = await generateCryptoKeyPair("RSASSA-PKCS1-v1_5");
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
        status VARCHAR(32) NOT NULL
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
    seedBridgeActorJwk(
      db,
      `${TEST_ORIGIN}actors/bridge`,
      JSON.stringify(await exportJwk(bridgeKeys.privateKey)),
      JSON.stringify(await exportJwk(bridgeKeys.publicKey)),
    );
    await writeFile(databasePath, Buffer.from(db.export()));
    pythonBridgeInternalUrl = await startPythonBridgeFixture(databasePath);
  } finally {
    db.close();
  }

  const config: GatewayConfig = {
    actorIdentifier: "bridge",
    actorName: "Bridge",
    actorSummary: "Bridge summary",
    pythonBridgeInternalUrl,
    fedifyOrigin: TEST_ORIGIN,
    port: 3000,
        pythonBridgeSharedSecret: "secret",
    logLevel: "info",
  };

  const activity = new Follow({
    id: new URL(`${REMOTE_ORIGIN}activities/follow/1`),
    actor: new URL(`${REMOTE_ORIGIN}u/bob`),
    object: new URL(`${TEST_ORIGIN}communities/hackers`),
  });
  Object.assign(activity, {
    getActor: async () => ({
      inboxId: new URL(`${REMOTE_ORIGIN}u/bob/inbox`),
    }),
  });

  const event = await buildLocalFollowRequestedEvent(config, activity);

  assert.ok(event != null);
  assert.equal(event?.event_type, "local.follow_requested");
  assert.equal(event?.community_actor_id, `${TEST_ORIGIN}communities/hackers`);
  assert.equal(event?.actor_id, `${REMOTE_ORIGIN}u/bob`);
  assert.equal(event?.object.follow_activity_id, `${REMOTE_ORIGIN}activities/follow/1`);
  assert.equal(event?.object.remote_inbox_url, `${REMOTE_ORIGIN}u/bob/inbox`);
  console.log("verify:local-community-follow passed");
}

await main();
