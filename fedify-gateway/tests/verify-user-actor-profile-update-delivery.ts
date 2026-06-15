import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { exportJwk } from "@fedify/fedify";

import type { GatewayConfig } from "../src/config.js";
import { createGatewayApp } from "../src/server.js";
import initSqlJs, { seedBridgeActorJwk } from "./support/sqlite-fixture.js";
import {
  closeAllPythonBridgeFixtures,
  importFixedRsaKeyPair,
  startPythonBridgeFixture,
} from "./support/python-bridge-fixture.js";

const ORIGIN = "https://discord-bridge.example.com/";

/** Verify canonical immutable profile rendering and endpoint-batch validation. */
async function main(): Promise<void> {
  const bridgeKeys = await importFixedRsaKeyPair();
  const userKeys = await importFixedRsaKeyPair();
  const config = await buildConfig(bridgeKeys, userKeys);
  const app = createGatewayApp(config);
  const snapshot = {
    schemaVersion: 1,
    actorId: `${ORIGIN}actors/alice`,
    username: "alice",
    displayName: "Alice Example",
    summary: "Bridge operator",
    inboxId: `${ORIGIN}actors/alice/inbox`,
    outboxId: `${ORIGIN}actors/alice/outbox`,
    followersId: `${ORIGIN}actors/alice/followers`,
    publicKeyId: `${ORIGIN}actors/alice#main-key`,
    publicKeyPem: await exportPublicKeyPem(userKeys.publicKey),
    fields: [{ name: "Site", value: "https://example.com" }],
    avatar: null,
    header: null,
  };
  const activityId = `${ORIGIN}activities/user-profile-update/1/1`;

  const rendered = await app.request("/render-user-actor-profile-update", {
    method: "POST",
    headers: {
      Authorization: "Bearer secret",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ activityId, profileSnapshot: snapshot }),
  });
  assert.equal(rendered.status, 200);
  const renderedBody = await rendered.json() as {
    activityJson: Record<string, unknown>;
  };
  const activity = renderedBody.activityJson;
  const object = activity.object as Record<string, unknown>;
  assert.equal(activity.id, activityId);
  assert.equal(activity.type, "Update");
  assert.equal(activity.actor, snapshot.actorId);
  assert.equal(object.id, snapshot.actorId);
  assert.equal(object.type, "Person");
  assert.equal(object.name, snapshot.displayName);
  assert.equal(object.preferredUsername, snapshot.username);
  assert.ok(activity.to === "as:Public" || activity.to === "https://www.w3.org/ns/activitystreams#Public");
  assert.ok(activity.cc === snapshot.followersId || (Array.isArray(activity.cc) && activity.cc.includes(snapshot.followersId)));
  assert.equal(JSON.stringify(activity).includes("PRIVATE KEY"), false);

  const duplicateEndpoint = await app.request("/send-user-actor-deliveries", {
    method: "POST",
    headers: {
      Authorization: "Bearer secret",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      signingActorUrl: snapshot.actorId,
      actorUsername: snapshot.username,
      deliveries: [
        {
          deliveryId: 1,
          targetEndpointUrl: "https://remote.example/inbox",
          activityJson: activity,
        },
        {
          deliveryId: 2,
          targetEndpointUrl: "https://remote.example/inbox",
          activityJson: activity,
        },
      ],
    }),
  });
  assert.equal(duplicateEndpoint.status, 400);

  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const url = input instanceof URL
      ? input.href
      : typeof input === "string"
        ? input
        : input.url;
    if (url.startsWith(config.pythonBridgeInternalUrl)) {
      return originalFetch(input, init);
    }
    if (url === "https://healthy.example/inbox") {
      return new Response("accepted", { status: 202 });
    }
    if (url === "https://failed.example/inbox") {
      return new Response("SECRET_RESPONSE_BODY", { status: 503 });
    }
    throw new Error(`unexpected fetch ${url}`);
  };
  try {
    const deliveryResponse = await app.request("/send-user-actor-deliveries", {
      method: "POST",
      headers: {
        Authorization: "Bearer secret",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        signingActorUrl: snapshot.actorId,
        actorUsername: snapshot.username,
        deliveries: [
          {
            deliveryId: 10,
            targetEndpointUrl: "https://healthy.example/inbox",
            activityJson: activity,
          },
          {
            deliveryId: 11,
            targetEndpointUrl: "https://failed.example/inbox",
            activityJson: activity,
          },
        ],
      }),
    });
    assert.equal(deliveryResponse.status, 200);
    const deliveryBody = await deliveryResponse.json() as {
      outcomes: Array<{ deliveryId: number; ok: boolean; error: string | null }>;
    };
    assert.deepEqual(
      deliveryBody.outcomes.map((outcome) => [outcome.deliveryId, outcome.ok]),
      [[10, true], [11, false]],
    );
    assert.equal(
      deliveryBody.outcomes[1]?.error?.includes("SECRET_RESPONSE_BODY"),
      false,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }

  console.log("verify:user-actor-profile-update-delivery passed");
}

async function buildConfig(
  bridgeKeys: CryptoKeyPair,
  userKeys: CryptoKeyPair,
): Promise<GatewayConfig> {
  const sqlJs = await initSqlJs();
  const tempDir = await mkdtemp(path.join(tmpdir(), "profile-update-delivery-"));
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
        private_key_pem TEXT NOT NULL,
        display_name VARCHAR(30),
        summary VARCHAR(500),
        avatar_media_id INTEGER,
        header_media_id INTEGER
      )
    `);
    db.run(
      `INSERT INTO users (
        discord_user_id, activitypub_username, actor_url, inbox_url, outbox_url,
        followers_url, public_key_pem, private_key_pem, display_name, summary
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      [
        "1",
        "alice",
        `${ORIGIN}actors/alice`,
        `${ORIGIN}actors/alice/inbox`,
        `${ORIGIN}actors/alice/outbox`,
        `${ORIGIN}actors/alice/followers`,
        await exportPublicKeyPem(userKeys.publicKey),
        await exportPrivateKeyPem(userKeys.privateKey),
        "Current value is intentionally ignored",
        "Current summary is intentionally ignored",
      ],
    );
    db.run(`
      CREATE TABLE user_profile_fields (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        position INTEGER NOT NULL,
        name VARCHAR(255) NOT NULL,
        value VARCHAR(255) NOT NULL
      )
    `);
    db.run(`
      CREATE TABLE profile_media_assets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id VARCHAR(64) NOT NULL,
        storage_key VARCHAR(512) NOT NULL,
        content_type VARCHAR(64) NOT NULL,
        width INTEGER NOT NULL,
        height INTEGER NOT NULL,
        byte_size INTEGER NOT NULL,
        sha256 VARCHAR(64) NOT NULL,
        created_at DATETIME NOT NULL
      )
    `);
    seedBridgeActorJwk(
      db,
      `${ORIGIN}actors/bridge`,
      JSON.stringify(await exportJwk(bridgeKeys.privateKey)),
      JSON.stringify(await exportJwk(bridgeKeys.publicKey)),
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
    fedifyOrigin: ORIGIN,
    port: 3000,
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

try {
  await main();
} finally {
  await closeAllPythonBridgeFixtures();
}
