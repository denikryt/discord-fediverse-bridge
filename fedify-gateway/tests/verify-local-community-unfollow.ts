import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { Follow, Undo } from "@fedify/vocab";
import { exportJwk } from "@fedify/fedify";
import initSqlJs, { seedBridgeActorJwk } from "./support/sqlite-fixture.js";
import { closeAllPythonBridgeFixtures, importFixedRsaKeyPair, startPythonBridgeFixture } from "./support/python-bridge-fixture.js";

import { buildLocalUnfollowRequestedEvent } from "../src/federation.js";
import type { GatewayConfig } from "../src/config.js";

const TEST_ORIGIN = "https://discord-bridge.example.com/";
const REMOTE_ORIGIN = "https://lemmy.example/";

async function buildConfig(): Promise<GatewayConfig> {
  const sqlJs = await initSqlJs();
  const tempDir = await mkdtemp(path.join(tmpdir(), "fedify-local-unfollow-"));
  const databasePath = path.join(tempDir, "bridge.db");
  let pythonBridgeInternalUrl = "";
  const bridgeKeys = await importFixedRsaKeyPair();
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
        `${TEST_ORIGIN}actors/communities/hackers`,
        `${TEST_ORIGIN}actors/communities/hackers/inbox`,
        `${TEST_ORIGIN}actors/communities/hackers/outbox`,
        `${TEST_ORIGIN}actors/communities/hackers/followers`,
        "public-key",
        "private-key",
        "active",
      ],
    );
    seedBridgeActorJwk(
      db,
      `${TEST_ORIGIN}actors/services/bridge`,
      JSON.stringify(await exportJwk(bridgeKeys.privateKey)),
      JSON.stringify(await exportJwk(bridgeKeys.publicKey)),
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
  const config = await buildConfig();
  const rawUndo = {
    id: `${REMOTE_ORIGIN}activities/undo/1`,
    type: "Undo",
    actor: `${REMOTE_ORIGIN}u/bob`,
    object: {
      id: `${REMOTE_ORIGIN}activities/follow/1`,
      type: "Follow",
      actor: `${REMOTE_ORIGIN}u/bob`,
      object: `${TEST_ORIGIN}actors/communities/hackers`,
    },
  };
  const activity = new Undo({
    id: new URL(rawUndo.id), actor: new URL(rawUndo.actor),
    object: new Follow({
      id: new URL(rawUndo.object.id), actor: new URL(rawUndo.object.actor),
      object: new URL(rawUndo.object.object),
    }),
  });

  const event = await buildLocalUnfollowRequestedEvent(config, activity);

  assert.ok(event != null);
  assert.equal(event?.event_type, "local.unfollow_requested");
  assert.equal(event?.target_actor_id, `${TEST_ORIGIN}actors/communities/hackers`);
  assert.equal(event?.target_actor_kind, "community");
  assert.equal(event?.actor_id, `${REMOTE_ORIGIN}u/bob`);
  assert.equal(event?.object.follow_activity_id, `${REMOTE_ORIGIN}activities/follow/1`);

  // Untrusted raw JSON may be poisoned under the same Undo.id but must not
  // override a typed authenticated Undo(Follow) or its original destination.
  const poisonedRaw = await buildLocalUnfollowRequestedEvent({
    ...config, activitypubRawJson: {
      ...rawUndo, object: { ...rawUndo.object, actor: `${REMOTE_ORIGIN}u/alice` },
    },
  }, activity);
  assert.equal(poisonedRaw?.object.follow_activity_id, rawUndo.object.id);

  const correctOwner = await buildLocalUnfollowRequestedEvent({
    ...config, activitypubInboxOwner: {
      kind: "community", identifier: "hackers",
      actorUrl: `${TEST_ORIGIN}actors/communities/hackers`,
    },
  }, activity);
  assert.equal(correctOwner?.object.follow_activity_id, rawUndo.object.id);
  const foreignOwner = await buildLocalUnfollowRequestedEvent({
    ...config, activitypubInboxOwner: {
      kind: "community", identifier: "foreign",
      actorUrl: `${TEST_ORIGIN}actors/communities/foreign`,
    },
  }, activity);
  assert.equal(foreignOwner, null, "wrong actor-specific route owner must not authorize Undo");

  const mismatchedActor = await buildLocalUnfollowRequestedEvent(config, new Undo({
    id: new URL(rawUndo.id), actor: new URL(rawUndo.actor),
    object: new Follow({
      id: new URL(rawUndo.object.id), actor: new URL(`${REMOTE_ORIGIN}u/alice`),
      object: new URL(rawUndo.object.object),
    }),
  }));
  assert.equal(mismatchedActor, null);

  const remoteTarget = await buildLocalUnfollowRequestedEvent(config, new Undo({
    id: new URL(rawUndo.id), actor: new URL(rawUndo.actor),
    object: new Follow({
      id: new URL(rawUndo.object.id), actor: new URL(rawUndo.object.actor),
      object: new URL(`${REMOTE_ORIGIN}c/remote`),
    }),
  }));
  assert.equal(remoteTarget, null);

  const unsupportedObject = await buildLocalUnfollowRequestedEvent(config, new Undo({
    id: new URL(rawUndo.id), actor: new URL(rawUndo.actor),
    object: new URL(rawUndo.object.id),
  }));
  assert.equal(unsupportedObject, null);

  console.log("verify:local-community-unfollow passed");
}

try {
  await main();
} finally {
  await closeAllPythonBridgeFixtures();
}
