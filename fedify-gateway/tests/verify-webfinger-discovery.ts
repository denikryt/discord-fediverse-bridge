import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { webcrypto } from "node:crypto";

import { exportJwk, generateCryptoKeyPair } from "@fedify/fedify";
import initSqlJs from "sql.js";

import { type GatewayConfig } from "../src/config.js";
import { createGatewayApp } from "../src/server.js";

const TEST_ORIGIN = "https://discord-bridge.example.com/";

async function main(): Promise<void> {
  const sqlJs = await initSqlJs();
  const tempDir = await mkdtemp(path.join(tmpdir(), "fedify-webfinger-"));
  const databasePath = path.join(tempDir, "bridge.db");
  const bridgeKeys = await generateCryptoKeyPair("RSASSA-PKCS1-v1_5");
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
        `${TEST_ORIGIN}users/alice`,
        `${TEST_ORIGIN}users/alice/inbox`,
        `${TEST_ORIGIN}users/alice/outbox`,
        `${TEST_ORIGIN}users/alice/followers`,
        await exportPublicKeyPem(bridgeKeys.publicKey),
        await exportPrivateKeyPem(bridgeKeys.privateKey),
      ],
    );
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
        await exportPublicKeyPem(bridgeKeys.publicKey),
        await exportPrivateKeyPem(bridgeKeys.privateKey),
        "active",
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

  const app = createGatewayApp(config);

  await assertWebFingerSelfLink(
    app,
    `acct:bridge@discord-bridge.example.com`,
    `${TEST_ORIGIN}actors/bridge`,
  );
  await assertWebFingerSelfLink(
    app,
    `acct:alice@discord-bridge.example.com`,
    `${TEST_ORIGIN}users/alice`,
  );
  await assertWebFingerSelfLink(
    app,
    `acct:!hackers@discord-bridge.example.com`,
    `${TEST_ORIGIN}communities/hackers`,
  );
  await assertWebFingerSelfLink(
    app,
    `acct:hackers@discord-bridge.example.com`,
    `${TEST_ORIGIN}communities/hackers`,
  );

  const communityAliasResponse = await app.request(
    "/.well-known/webfinger?resource=acct:hackers@discord-bridge.example.com",
  );
  assert.equal(communityAliasResponse.status, 200);

  const missingUserResponse = await app.request(
    "/.well-known/webfinger?resource=acct:missing@discord-bridge.example.com",
  );
  assert.equal(missingUserResponse.status, 404);

  const wrongHostResponse = await app.request(
    "/.well-known/webfinger?resource=acct:alice@wrong-host.example.com",
  );
  assert.equal(wrongHostResponse.status, 404);

  const missingResourceResponse = await app.request("/.well-known/webfinger");
  assert.equal(missingResourceResponse.status, 400);

  console.log("verify:webfinger-discovery passed");
}

async function assertWebFingerSelfLink(
  app: ReturnType<typeof createGatewayApp>,
  resource: string,
  expectedHref: string,
): Promise<void> {
  const response = await app.request(
    `/.well-known/webfinger?resource=${encodeURIComponent(resource)}`,
  );

  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-type"), "application/jrd+json");

  const payload = await response.json();
  assert.equal(payload.subject, resource);
  assert.equal(payload.links[0]?.href, expectedHref);
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
