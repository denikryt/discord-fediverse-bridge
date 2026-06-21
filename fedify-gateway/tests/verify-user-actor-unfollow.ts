import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { Undo } from "@fedify/vocab";
import initSqlJs from "./support/sqlite-fixture.js";
import { closeAllPythonBridgeFixtures, startPythonBridgeFixture } from "./support/python-bridge-fixture.js";

import type { GatewayConfig } from "../src/config.js";
import { buildLocalUnfollowRequestedEvent } from "../src/federation.js";

const TEST_ORIGIN = "https://discord-bridge.example.com/";
const REMOTE_ORIGIN = "https://remote.example/";

/** Verify that Undo(Follow) preserves actor and target ownership for a local Person. */
async function main(): Promise<void> {
  const config = await buildConfig();
  const rawUndo = {
    id: `${REMOTE_ORIGIN}activities/undo/1`,
    type: "Undo",
    actor: `${REMOTE_ORIGIN}users/bob`,
    object: {
      id: `${REMOTE_ORIGIN}activities/follow/1`,
      type: "Follow",
      actor: `${REMOTE_ORIGIN}users/bob`,
      object: `${TEST_ORIGIN}actors/users/alice`,
    },
  };
  const activity = new Undo({ id: new URL(rawUndo.id), actor: new URL(rawUndo.actor) });

  const event = await buildLocalUnfollowRequestedEvent(config, activity, rawUndo);
  assert.ok(event != null);
  assert.equal(event.target_actor_kind, "user");
  assert.equal(event.target_actor_id, `${TEST_ORIGIN}actors/users/alice`);
  assert.equal(event.actor_id, `${REMOTE_ORIGIN}users/bob`);
  assert.equal(event.object.follow_activity_id, `${REMOTE_ORIGIN}activities/follow/1`);

  const wrongActor = await buildLocalUnfollowRequestedEvent(config, activity, {
    ...rawUndo,
    object: { ...rawUndo.object, actor: `${REMOTE_ORIGIN}users/mallory` },
  });
  assert.equal(wrongActor, null);

  const wrongTarget = await buildLocalUnfollowRequestedEvent(config, activity, {
    ...rawUndo,
    object: { ...rawUndo.object, object: `${TEST_ORIGIN}actors/missing` },
  });
  assert.equal(wrongTarget, null);

  console.log("verify:user-actor-unfollow passed");
}

/** Build the minimum persisted registered-user identity used by Undo normalization. */
async function buildConfig(): Promise<GatewayConfig> {
  const sqlJs = await initSqlJs();
  const tempDir = await mkdtemp(path.join(tmpdir(), "fedify-user-unfollow-"));
  const databasePath = path.join(tempDir, "bridge.db");
  const db = new sqlJs.Database();
  try {
    db.run(`CREATE TABLE users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      discord_user_id VARCHAR(255) NOT NULL,
      activitypub_username VARCHAR(255) NOT NULL,
      actor_url VARCHAR(512) NOT NULL,
      inbox_url VARCHAR(512) NOT NULL,
      outbox_url VARCHAR(512) NOT NULL,
      followers_url VARCHAR(512) NOT NULL,
      public_key_pem TEXT NOT NULL,
      private_key_pem TEXT NOT NULL
    )`);
    db.run(
      `INSERT INTO users (discord_user_id, activitypub_username, actor_url, inbox_url, outbox_url, followers_url, public_key_pem, private_key_pem)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
      ["1", "alice", `${TEST_ORIGIN}actors/users/alice`, `${TEST_ORIGIN}actors/users/alice/inbox`, `${TEST_ORIGIN}actors/users/alice/outbox`, `${TEST_ORIGIN}actors/users/alice/followers`, "public", "private"],
    );
    await writeFile(databasePath, Buffer.from(db.export()));
  } finally {
    db.close();
  }
  return {
    actorIdentifier: "bridge",
    actorName: "Bridge",
    actorSummary: "Bridge summary",
    pythonBridgeInternalUrl: await startPythonBridgeFixture(databasePath),
    fedifyOrigin: TEST_ORIGIN,
    port: 3000,
    pythonBridgeSharedSecret: "secret",
    logLevel: "info",
  };
}

try {
  await main();
} finally {
  await closeAllPythonBridgeFixtures();
}
