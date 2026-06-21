import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { Follow } from "@fedify/vocab";
import initSqlJs from "./support/sqlite-fixture.js";
import { closeAllPythonBridgeFixtures, startPythonBridgeFixture } from "./support/python-bridge-fixture.js";

import type { GatewayConfig } from "../src/config.js";
import { buildLocalFollowRequestedEvent } from "../src/federation.js";

const TEST_ORIGIN = "https://discord-bridge.example.com/";
const REMOTE_ORIGIN = "https://remote.example/";

/** Verify that a Follow targeting a registered Person is normalized for the user domain. */
async function main(): Promise<void> {
  const config = await buildConfig();
  const activity = new Follow({
    id: new URL(`${REMOTE_ORIGIN}activities/follow/1`),
    actor: new URL(`${REMOTE_ORIGIN}users/bob`),
    object: new URL(`${TEST_ORIGIN}actors/users/alice`),
  });
  Object.assign(activity, {
    getActor: async () => ({
      id: new URL(`${REMOTE_ORIGIN}users/bob`),
      inboxId: new URL(`${REMOTE_ORIGIN}users/bob/inbox`),
      endpoints: { sharedInbox: `${REMOTE_ORIGIN}inbox` },
    }),
  });

  const event = await buildLocalFollowRequestedEvent(config, activity);
  assert.ok(event != null);
  assert.equal(event.target_actor_kind, "user");
  assert.equal(event.target_actor_id, `${TEST_ORIGIN}actors/users/alice`);
  assert.equal(event.actor_id, `${REMOTE_ORIGIN}users/bob`);
  assert.equal(event.object.follow_activity_id, `${REMOTE_ORIGIN}activities/follow/1`);
  assert.equal(event.object.remote_inbox_url, `${REMOTE_ORIGIN}users/bob/inbox`);
  assert.equal(event.object.remote_shared_inbox_url, `${REMOTE_ORIGIN}inbox`);


  console.log("verify:user-actor-follow passed");
}

/** Build the minimum persisted registered-user identity used by actor-kind resolution. */
async function buildConfig(): Promise<GatewayConfig> {
  const sqlJs = await initSqlJs();
  const tempDir = await mkdtemp(path.join(tmpdir(), "fedify-user-follow-"));
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
