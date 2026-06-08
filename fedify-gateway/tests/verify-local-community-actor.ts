import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { webcrypto } from "node:crypto";

import { exportJwk, generateCryptoKeyPair } from "@fedify/fedify";
import initSqlJs, { seedBridgeActorJwk } from "./support/sqlite-fixture.js";
import { startPythonBridgeFixture } from "./support/python-bridge-fixture.js";

import { buildLocalCommunityGroupActor } from "../src/actors.js";
import {
  hasLocalActor,
  loadActorKeyPair,
  loadLocalCommunityIdentity,
} from "../src/actor-store.js";
import type { GatewayConfig } from "../src/config.js";

const TEST_ORIGIN = "https://discord-bridge.example.com/";

async function main(): Promise<void> {
  const sqlJs = await initSqlJs();
  const tempDir = await mkdtemp(path.join(tmpdir(), "fedify-local-community-"));
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
        await exportPublicKeyPem(communityKeys.publicKey),
        await exportPrivateKeyPem(communityKeys.privateKey),
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

  const identity = await loadLocalCommunityIdentity(config, "hackers");
  const keyPair = await loadActorKeyPair(config, "hackers");

  assert.ok(identity != null);
  assert.equal(identity?.actorId.href, `${TEST_ORIGIN}communities/hackers`);
  assert.equal(identity?.displayName, "Hackers");
  assert.equal(await hasLocalActor(config, "hackers"), true);
  assert.ok(keyPair != null);

  const actor = buildLocalCommunityGroupActor(
    identity,
    new URL(`${TEST_ORIGIN}inbox`),
    [],
  );
  assert.equal(actor.id?.href, `${TEST_ORIGIN}communities/hackers`);
  assert.equal(actor.name, "Hackers");
  assert.equal(actor.followersId?.href, `${TEST_ORIGIN}communities/hackers/followers`);
  console.log("verify:local-community-actor passed");
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
