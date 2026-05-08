import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { webcrypto } from "node:crypto";

import { exportJwk, generateCryptoKeyPair } from "@fedify/fedify";
import initSqlJs from "sql.js";

import {
  buildBridgeServiceActor,
  buildUserPersonActor,
} from "../src/actors.js";
import {
  getBridgeActorIdentity,
  hasLocalActor,
  loadActorKeyPair,
  loadUserActorIdentity,
} from "../src/actor-store.js";
import type { GatewayConfig } from "../src/config.js";

const TEST_ORIGIN = "https://discord-bridge.example.com/";

async function main(): Promise<void> {
  // The verification script uses a throwaway SQLite file so the gateway-side
  // actor store can be checked without depending on the real bridge database.
  const sqlJs = await initSqlJs();
  const tempDir = await mkdtemp(path.join(tmpdir(), "fedify-actor-layer-"));
  const databasePath = path.join(tempDir, "bridge.db");

  const bridgeKeys = await generateCryptoKeyPair("RSASSA-PKCS1-v1_5");
  const userKeys = await generateCryptoKeyPair("RSASSA-PKCS1-v1_5");
  const db = new sqlJs.Database();
  try {
    db.run(`
      CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        discord_user_id VARCHAR(64) NOT NULL,
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
        "1234567890",
        "alice",
        `${TEST_ORIGIN}users/alice`,
        `${TEST_ORIGIN}users/alice/inbox`,
        `${TEST_ORIGIN}users/alice/outbox`,
        `${TEST_ORIGIN}users/alice/followers`,
        await exportPublicKeyPem(userKeys.publicKey),
        await exportPrivateKeyPem(userKeys.privateKey),
      ],
    );
    await writeFile(databasePath, Buffer.from(db.export()));
  } finally {
    db.close();
  }

  const config: GatewayConfig = {
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

  const bridgeIdentity = getBridgeActorIdentity(config);
  const userIdentity = await loadUserActorIdentity(config, "alice");
  const userKeyPair = await loadActorKeyPair(config, "alice");
  const bridgeKeyPair = await loadActorKeyPair(config, "bridge");

  assert.equal(bridgeIdentity.actorId.href, `${TEST_ORIGIN}actors/bridge`);
  assert.equal(await hasLocalActor(config, "bridge"), true);
  assert.equal(await hasLocalActor(config, "alice"), true);
  assert.equal(await hasLocalActor(config, "missing"), false);
  assert.ok(userIdentity != null);
  assert.equal(userIdentity?.actorId.href, `${TEST_ORIGIN}users/alice`);
  assert.ok(userKeyPair != null);
  assert.ok(bridgeKeyPair != null);

  const bridgeActor = buildBridgeServiceActor(
    bridgeIdentity,
    new URL(`${TEST_ORIGIN}inbox`),
    [],
  );
  const userActor = buildUserPersonActor(
    userIdentity,
    new URL(`${TEST_ORIGIN}inbox`),
    [],
  );

  assert.equal(bridgeActor.id?.href, `${TEST_ORIGIN}actors/bridge`);
  assert.equal(userActor.id?.href, `${TEST_ORIGIN}users/alice`);
  assert.equal(userActor.inboxId?.href, `${TEST_ORIGIN}users/alice/inbox`);

  console.log("verify:actor-layer passed");
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
