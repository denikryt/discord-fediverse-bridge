/**
 * Gateway contract tests for local-community Update/Delete fanout.
 *
 * Discord-originated local-community edits/deletes must fan out as a
 * community-owned Announce(Update|Delete) to each accepted remote subscriber.
 */

import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { exportJwk, generateCryptoKeyPair } from "@fedify/fedify";
import initSqlJs, { seedBridgeActorJwk } from "./support/sqlite-fixture.js";
import { startPythonBridgeFixture } from "./support/python-bridge-fixture.js";

import { deleteContent, updateContent } from "../src/federation-outbound.js";
import type { GatewayConfig } from "../src/config.js";

const TEST_ORIGIN = "https://discord-bridge.example.com/";
const COMMUNITY_ACTOR = `${TEST_ORIGIN}communities/hackers`;
const USER_ACTOR = `${TEST_ORIGIN}actors/alice`;

interface DeliveryRecord {
  inboxId: string;
  payload: Record<string, unknown>;
}

async function main(): Promise<void> {
  await testLocalCommunityUpdateFansOutToAcceptedRemoteSubscribers();
  await testLocalCommunityDeleteFansOutToAcceptedRemoteSubscribers();
  console.log("verify:local-community-update-delete passed");
}

/**
 * Action: `/update` is called for a Discord-backed local community object.
 * Expected: every accepted remote subscriber receives Announce(Update(Note)).
 */
async function testLocalCommunityUpdateFansOutToAcceptedRemoteSubscribers(): Promise<void> {
  const config = await buildConfig();
  const deliveries: DeliveryRecord[] = [];
  const restoreFetch = installFetchRecorder(deliveries);

  await updateContent({} as never, config, {
    actorUsername: "alice",
    communityActorUrl: COMMUNITY_ACTOR,
    apObjectId: `${TEST_ORIGIN}users/alice/comment/1`,
    kind: "comment",
    bodyMarkdown: "Edited from Discord",
    title: null,
    inReplyToObjectId: `${TEST_ORIGIN}users/alice/post/1`,
  });
  restoreFetch();

  assert.deepEqual(
    deliveries.map((delivery) => delivery.inboxId).sort(),
    [
      "https://lemmy.example/u/alice/inbox",
      "https://mastodon.example/ap/users/bob/inbox",
    ],
  );
  for (const delivery of deliveries) {
    assert.equal(delivery.payload.type, "Announce");
    assert.equal(delivery.payload.actor, COMMUNITY_ACTOR);
    const embeddedUpdate = delivery.payload.object as Record<string, unknown>;
    const embeddedObject = embeddedUpdate.object as Record<string, unknown>;
    assert.equal(embeddedUpdate.type, "Update");
    assert.equal(embeddedUpdate.actor, USER_ACTOR);
    assert.equal(embeddedObject.type, "Note");
    assert.equal(embeddedObject.id, `${TEST_ORIGIN}users/alice/comment/1`);
    assert.equal(embeddedObject.attributedTo, USER_ACTOR);
  }
}

/**
 * Action: `/delete` is called for a Discord-backed local community object.
 * Expected: every accepted remote subscriber receives Announce(Delete(...)).
 */
async function testLocalCommunityDeleteFansOutToAcceptedRemoteSubscribers(): Promise<void> {
  const config = await buildConfig();
  const deliveries: DeliveryRecord[] = [];
  const restoreFetch = installFetchRecorder(deliveries);

  await deleteContent({} as never, config, {
    actorUsername: "alice",
    communityActorUrl: COMMUNITY_ACTOR,
    apObjectId: `${TEST_ORIGIN}users/alice/comment/1`,
  });
  restoreFetch();

  assert.equal(deliveries.length, 2);
  for (const delivery of deliveries) {
    assert.equal(delivery.payload.type, "Announce");
    assert.equal(delivery.payload.actor, COMMUNITY_ACTOR);
    const embeddedDelete = delivery.payload.object as Record<string, unknown>;
    assert.equal(embeddedDelete.type, "Delete");
    assert.equal(embeddedDelete.actor, USER_ACTOR);
    assert.equal(embeddedDelete.object, `${TEST_ORIGIN}users/alice/comment/1`);
  }
}

function installFetchRecorder(deliveries: DeliveryRecord[]): () => void {
  /** Capture exact signed JSON deliveries without external network dependency. */
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const targetUrl = input instanceof URL ? input.href : String(input);
    if (targetUrl.startsWith("http://127.0.0.1:")) return await originalFetch(input, init);
    deliveries.push({
      inboxId: input instanceof URL ? input.href : String(input),
      payload: JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>,
    });
    return new Response("", { status: 202, statusText: "Accepted" });
  };
  return () => {
    globalThis.fetch = originalFetch;
  };
}

async function buildConfig(): Promise<GatewayConfig> {
  /** Build a temporary SQLite snapshot with one local community and followers. */
  const sqlJs = await initSqlJs();
  const tempDir = await mkdtemp(path.join(tmpdir(), "fedify-local-community-update-delete-"));
  const databasePath = path.join(tempDir, "bridge.db");
  let pythonBridgeInternalUrl = "";
  const bridgeKeys = await generateCryptoKeyPair("RSASSA-PKCS1-v1_5");
  const communityKeys = await generateCryptoKeyPair("RSASSA-PKCS1-v1_5");
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
    db.run(`
      CREATE TABLE remote_subscribers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        local_community_id INTEGER NOT NULL,
        remote_actor_id VARCHAR(512) NOT NULL,
        remote_inbox_url VARCHAR(512) NOT NULL,
        follow_activity_id VARCHAR(512) NOT NULL,
        status VARCHAR(32) NOT NULL,
        delivery_profile VARCHAR(64) NOT NULL DEFAULT 'threadiverse_group',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
        COMMUNITY_ACTOR,
        `${COMMUNITY_ACTOR}/inbox`,
        `${COMMUNITY_ACTOR}/outbox`,
        `${COMMUNITY_ACTOR}/followers`,
        await exportPublicKeyPem(communityKeys.publicKey),
        await exportPrivateKeyPem(communityKeys.privateKey),
        "active",
      ],
    );
    db.run(
      `
        INSERT INTO remote_subscribers (
          local_community_id,
          remote_actor_id,
          remote_inbox_url,
          follow_activity_id,
          status,
          delivery_profile
        ) VALUES
          (1, 'https://lemmy.example/u/alice', 'https://lemmy.example/u/alice/inbox', 'follow-1', 'accepted', 'threadiverse_group'),
          (1, 'https://mastodon.example/ap/users/bob', 'https://mastodon.example/ap/users/bob/inbox', 'follow-2', 'accepted', 'mastodon_compat')
      `,
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

async function exportPrivateKeyPem(privateKey: CryptoKey): Promise<string> {
  return toPem("PRIVATE KEY", Buffer.from(await webcrypto.subtle.exportKey("pkcs8", privateKey)));
}

async function exportPublicKeyPem(publicKey: CryptoKey): Promise<string> {
  return toPem("PUBLIC KEY", Buffer.from(await webcrypto.subtle.exportKey("spki", publicKey)));
}

function toPem(label: string, bytes: Buffer): string {
  const base64 = bytes.toString("base64");
  const wrapped = base64.match(/.{1,64}/g)?.join("\n") ?? base64;
  return `-----BEGIN ${label}-----\n${wrapped}\n-----END ${label}-----\n`;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
