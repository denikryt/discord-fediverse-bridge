import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { Undo } from "@fedify/vocab";
import { exportJwk, generateCryptoKeyPair } from "@fedify/fedify";
import initSqlJs from "sql.js";

import { buildLocalUnfollowRequestedEvent } from "../src/federation.js";
import type { GatewayConfig } from "../src/config.js";

const TEST_ORIGIN = "https://discord-bridge.example.com/";
const REMOTE_ORIGIN = "https://lemmy.example/";

async function buildConfig(): Promise<GatewayConfig> {
  const sqlJs = await initSqlJs();
  const tempDir = await mkdtemp(path.join(tmpdir(), "fedify-local-unfollow-"));
  const databasePath = path.join(tempDir, "bridge.db");
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
    await writeFile(databasePath, Buffer.from(db.export()));
  } finally {
    db.close();
  }

  return {
    actorIdentifier: "bridge",
    actorName: "Bridge",
    actorSummary: "Bridge summary",
    bridgePrivateKeyJwkJson: JSON.stringify(await exportJwk(bridgeKeys.privateKey)),
    bridgePublicKeyJwkJson: JSON.stringify(await exportJwk(bridgeKeys.publicKey)),
    communityActorId: null,
    databaseUrl: `sqlite:///${databasePath}`,
    fedifyOrigin: TEST_ORIGIN,
    port: 3000,
    pythonBridgeEventsUrl: "http://127.0.0.1:8080/internal/activitypub/events",
    pythonBridgeSharedSecret: "secret",
    logLevel: "info",
  };
}

async function main(): Promise<void> {
  const config = await buildConfig();
  const rawUndo = {
    id: `${REMOTE_ORIGIN}activities/undo/1`,
    type: "Undo",
    actor: `${REMOTE_ORIGIN}u/bob`,
    object: {
      id: `${REMOTE_ORIGIN}activities/follow/1`,
      type: "Follow",
      actor: `${REMOTE_ORIGIN}u/bob`,
      object: `${TEST_ORIGIN}communities/hackers`,
    },
  };
  const activity = new Undo({
    id: new URL(rawUndo.id),
    actor: new URL(rawUndo.actor),
  });

  const event = await buildLocalUnfollowRequestedEvent(config, activity, rawUndo);

  assert.ok(event != null);
  assert.equal(event?.event_type, "local.unfollow_requested");
  assert.equal(event?.community_actor_id, `${TEST_ORIGIN}communities/hackers`);
  assert.equal(event?.actor_id, `${REMOTE_ORIGIN}u/bob`);
  assert.equal(event?.object.follow_activity_id, `${REMOTE_ORIGIN}activities/follow/1`);

  const mismatchedActor = await buildLocalUnfollowRequestedEvent(config, activity, {
    ...rawUndo,
    object: { ...rawUndo.object, actor: `${REMOTE_ORIGIN}u/alice` },
  });
  assert.equal(mismatchedActor, null);

  const remoteTarget = await buildLocalUnfollowRequestedEvent(config, activity, {
    ...rawUndo,
    object: { ...rawUndo.object, object: `${REMOTE_ORIGIN}c/remote` },
  });
  assert.equal(remoteTarget, null);

  const unsupportedObject = await buildLocalUnfollowRequestedEvent(config, activity, {
    ...rawUndo,
    object: `${REMOTE_ORIGIN}activities/follow/1`,
  });
  assert.equal(unsupportedObject, null);

  console.log("verify:local-community-unfollow passed");
}

await main();
