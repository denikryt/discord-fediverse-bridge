/**
 * Verifies Stage 3 fake remote actor dereference at the HTTP boundary.
 *
 * A remote Follow must resolve Bob's effective inbox data from Bob's served
 * actor document. The test deliberately does not patch `activity.getActor()` or
 * `buildLocalFollowRequestedEvent()` internals, matching the production failure
 * class that Stage 5 will exercise through public HTTP.
 */
import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { Follow } from "@fedify/vocab";
import initSqlJs from "./support/sqlite-fixture.js";
import {
  closeAllPythonBridgeFixtures,
  startPythonBridgeFixture,
} from "./support/python-bridge-fixture.js";
import { startRemoteActorFixture } from "./support/remote-actor-fixture.js";

import type { GatewayConfig } from "../src/config.js";
import { buildLocalFollowRequestedEvent } from "../src/federation.js";

const TEST_ORIGIN = "https://discord-bridge.example.com/";

/** Resolve Bob through HTTP and verify local.follow_requested inbox fields. */
async function main(): Promise<void> {
  const remote = await startRemoteActorFixture();
  try {
    const fetched = await fetch(remote.actorId, {
      headers: { Accept: "application/activity+json" },
    });
    assert.equal(fetched.status, 200, "remote fixture must serve Bob over HTTP");
    const actorDocument = await fetched.json() as Record<string, unknown>;
    assert.equal(actorDocument.id, remote.actorId);
    assert.equal(actorDocument.inbox, remote.inboxUrl);
    assert.deepEqual(actorDocument.endpoints, { sharedInbox: remote.sharedInboxUrl });

    const config = await buildConfig();
    const activity = new Follow({
      id: new URL(`${remote.origin}/activities/follow/1`),
      actor: new URL(remote.actorId),
      object: new URL(`${TEST_ORIGIN}actors/users/alice`),
    }, {
      documentLoader: remote.documentLoader,
      contextLoader: remote.documentLoader,
    });

    const event = await buildLocalFollowRequestedEvent(config, activity, {
      allowHttpLoopbackForTests: true,
    });
    assert.ok(event != null);
    assert.equal(event.target_actor_kind, "user");
    assert.equal(event.target_actor_id, `${TEST_ORIGIN}actors/users/alice`);
    assert.equal(event.actor_id, remote.actorId);
    assert.equal(event.object.follow_activity_id, `${remote.origin}/activities/follow/1`);
    assert.equal(event.object.remote_inbox_url, remote.inboxUrl);
    assert.equal(event.object.remote_shared_inbox_url, remote.sharedInboxUrl);
    await assertMismatchedDereferencedActorIsRejected(config);
    await assertPrivateEndpointIsRejected(config);
  } finally {
    await remote.close();
    await closeAllPythonBridgeFixtures();
  }

  console.log("verify:remote-actor-dereference-fixture passed");
}

/** Prove the Follow actor principal must match the fetched actor document id. */
async function assertMismatchedDereferencedActorIsRejected(config: GatewayConfig): Promise<void> {
  const hostile = await startRemoteActorFixture({
    actorDocumentId: "https://other.example/users/mallory",
  });
  try {
    const activity = new Follow({
      id: new URL(`${hostile.origin}/activities/follow/confused`),
      actor: new URL(hostile.actorId),
      object: new URL(`${TEST_ORIGIN}actors/users/alice`),
    }, {
      documentLoader: hostile.documentLoader,
      contextLoader: hostile.documentLoader,
    });
    const event = await buildLocalFollowRequestedEvent(config, activity, {
      allowHttpLoopbackForTests: true,
    });
    assert.equal(event, null, "Follow actor and dereferenced actor id must match");
  } finally {
    await hostile.close();
  }
}

/** Prove actor-document inboxes still pass through outbound endpoint admission. */
async function assertPrivateEndpointIsRejected(config: GatewayConfig): Promise<void> {
  const hostile = await startRemoteActorFixture({
    inboxUrl: "http://169.254.169.254/latest/meta-data",
    sharedInboxUrl: null,
  });
  try {
    const activity = new Follow({
      id: new URL(`${hostile.origin}/activities/follow/ssrf`),
      actor: new URL(hostile.actorId),
      object: new URL(`${TEST_ORIGIN}actors/users/alice`),
    }, {
      documentLoader: hostile.documentLoader,
      contextLoader: hostile.documentLoader,
    });
    const event = await buildLocalFollowRequestedEvent(config, activity, {
      allowHttpLoopbackForTests: true,
    });
    assert.equal(event, null, "private metadata endpoints must not produce a Python event");
  } finally {
    await hostile.close();
  }
}

/** Build a pre-cutover registered user row for target actor resolution. */
async function buildConfig(): Promise<GatewayConfig> {
  const sqlJs = await initSqlJs();
  const tempDir = await mkdtemp(path.join(tmpdir(), "fedify-remote-deref-"));
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

await main();
