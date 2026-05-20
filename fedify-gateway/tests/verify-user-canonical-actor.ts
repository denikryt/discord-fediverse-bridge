import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { webcrypto } from "node:crypto";

import { exportJwk, generateCryptoKeyPair } from "@fedify/fedify";
import initSqlJs from "sql.js";

import type { GatewayConfig } from "../src/config.js";
import { createGatewayApp } from "../src/server.js";

const TEST_ORIGIN = "https://discord-bridge.example.com/";

/**
 * Verify that registered-user actor documents expose one canonical /actors id.
 *
 * This regression protects Mastodon fetch-time actor validation: the DB stores
 * users under /actors/{username}, and /users/{username} must only resolve as a
 * compatibility entry point that returns the same canonical actor.
 */
async function main(): Promise<void> {
  const bridgeKeys = await generateCryptoKeyPair("RSASSA-PKCS1-v1_5");
  const userKeys = await generateCryptoKeyPair("RSASSA-PKCS1-v1_5");
  const config = await buildConfig(bridgeKeys, userKeys);
  const app = createGatewayApp(config);

  await assertCanonicalUserActor(app, "/actors/alice");
  await assertCanonicalUserActor(app, "/users/alice");

  console.log("verify:user-canonical-actor passed");
}

async function assertCanonicalUserActor(
  app: ReturnType<typeof createGatewayApp>,
  pathName: string,
): Promise<void> {
  const response = await app.request(pathName, {
    headers: { Accept: "application/activity+json" },
  });
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /application\/activity\+json/);

  const actor = await response.json() as Record<string, unknown>;
  const publicKey = actor.publicKey as Record<string, unknown>;

  assert.equal(actor.id, `${TEST_ORIGIN}actors/alice`);
  assert.equal(actor.type, "Person");
  assert.equal(actor.preferredUsername, "alice");
  assert.equal(actor.inbox, `${TEST_ORIGIN}actors/alice/inbox`);
  assert.equal(actor.outbox, `${TEST_ORIGIN}actors/alice/outbox`);
  assert.equal(actor.followers, `${TEST_ORIGIN}actors/alice/followers`);
  assert.equal(publicKey.id, `${TEST_ORIGIN}actors/alice#main-key`);
  assert.equal(publicKey.owner, `${TEST_ORIGIN}actors/alice`);
}

async function buildConfig(
  bridgeKeys: CryptoKeyPair,
  userKeys: CryptoKeyPair,
): Promise<GatewayConfig> {
  const sqlJs = await initSqlJs();
  const tempDir = await mkdtemp(path.join(tmpdir(), "user-canonical-actor-"));
  const databasePath = path.join(tempDir, "bridge.db");
  const db = new sqlJs.Database();

  try {
    db.run(`
      CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        discord_user_id VARCHAR(255) NOT NULL,
        activitypub_username VARCHAR(255) NOT NULL,
        actor_url VARCHAR(512) NOT NULL,
        inbox_url VARCHAR(512) NOT NULL,
        outbox_url VARCHAR(512) NOT NULL,
        followers_url VARCHAR(512) NOT NULL,
        public_key_pem TEXT NOT NULL,
        private_key_pem TEXT NOT NULL
      )
    `);
    db.run(
      `
        INSERT INTO users (
          discord_user_id,
          activitypub_username,
          actor_url,
          inbox_url,
          outbox_url,
          followers_url,
          public_key_pem,
          private_key_pem
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      `,
      [
        "123",
        "alice",
        `${TEST_ORIGIN}actors/alice`,
        `${TEST_ORIGIN}actors/alice/inbox`,
        `${TEST_ORIGIN}actors/alice/outbox`,
        `${TEST_ORIGIN}actors/alice/followers`,
        await exportPublicKeyPem(userKeys.publicKey),
        await exportPrivateKeyPem(userKeys.privateKey),
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

async function exportPrivateKeyPem(privateKey: CryptoKey): Promise<string> {
  return toPem(
    "PRIVATE KEY",
    Buffer.from(await webcrypto.subtle.exportKey("pkcs8", privateKey)),
  );
}

async function exportPublicKeyPem(publicKey: CryptoKey): Promise<string> {
  return toPem(
    "PUBLIC KEY",
    Buffer.from(await webcrypto.subtle.exportKey("spki", publicKey)),
  );
}

function toPem(label: string, bytes: Buffer): string {
  const base64 = bytes.toString("base64");
  const wrapped = base64.match(/.{1,64}/g)?.join("\n") ?? base64;
  return `-----BEGIN ${label}-----\n${wrapped}\n-----END ${label}-----\n`;
}

await main();
