import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { webcrypto } from "node:crypto";
import { tmpdir } from "node:os";
import path from "node:path";

import { exportJwk } from "@fedify/fedify";
import initSqlJs from "./support/sqlite-fixture.js";
import { closeAllPythonBridgeFixtures, importFixedRsaKeyPair, startPythonBridgeFixture } from "./support/python-bridge-fixture.js";

import { loadActorKeyPair } from "../src/actor-store.js";
import type { GatewayConfig } from "../src/config.js";

async function main(): Promise<void> {
  // The verifier proves both supported persisted formats and missing-state failure.
  const keys = await importFixedRsaKeyPair();
  const jwkPair = {
    publicData: JSON.stringify(await exportJwk(keys.publicKey)),
    privateData: JSON.stringify(await exportJwk(keys.privateKey)),
  };
  const pemPair = {
    publicData: toPem("PUBLIC KEY", Buffer.from(await webcrypto.subtle.exportKey("spki", keys.publicKey))),
    privateData: toPem("PRIVATE KEY", Buffer.from(await webcrypto.subtle.exportKey("pkcs8", keys.privateKey))),
  };
  await verifyStoredPair("jwk", jwkPair.publicData, jwkPair.privateData);
  await verifyStoredPair("pem", pemPair.publicData, pemPair.privateData);

  const emptyPath = await createDatabase(null);
  const emptyConfig = configFor(emptyPath);
  await assert.rejects(
    () => loadActorKeyPair(emptyConfig, "bridge"),
    /404 Not Found/,
  );
  console.log("verify:bridge-actor-key-store passed");
}

async function verifyStoredPair(
  keyFormat: "jwk" | "pem",
  publicData: string,
  privateData: string,
): Promise<void> {
  const pythonBridgeInternalUrl = await createDatabase({ keyFormat, publicData, privateData });
  assert.ok(await loadActorKeyPair(configFor(pythonBridgeInternalUrl), "bridge"));
}

async function createDatabase(
  row: { keyFormat: "jwk" | "pem"; publicData: string; privateData: string } | null,
): Promise<string> {
  const sqlJs = await initSqlJs();
  const tempDir = await mkdtemp(path.join(tmpdir(), "bridge-key-store-"));
  const databasePath = path.join(tempDir, "bridge.db");
  let pythonBridgeInternalUrl = "";
  const database = new sqlJs.Database();
  try {
    database.run(`
      CREATE TABLE bridge_actor_keys (
        actor_url TEXT NOT NULL,
        key_id TEXT NOT NULL,
        key_format TEXT NOT NULL,
        algorithm TEXT NOT NULL,
        public_key_data TEXT NOT NULL,
        private_key_data TEXT NOT NULL
      )
    `);
    if (row != null) {
      database.run(
        `INSERT INTO bridge_actor_keys VALUES (?, ?, ?, 'RSASSA-PKCS1-v1_5', ?, ?)`,
        [
          "https://bridge.example/actors/bridge",
          "https://bridge.example/actors/bridge#main-key",
          row.keyFormat,
          row.publicData,
          row.privateData,
        ],
      );
    }
    await writeFile(databasePath, Buffer.from(database.export()));
    pythonBridgeInternalUrl = await startPythonBridgeFixture(databasePath);
  } finally {
    database.close();
  }
  return pythonBridgeInternalUrl;
}

function configFor(pythonBridgeInternalUrl: string): GatewayConfig {
  return {
    actorIdentifier: "bridge",
    actorName: "Bridge",
    actorSummary: "Bridge",
    pythonBridgeInternalUrl,
    fedifyOrigin: "https://bridge.example/",
    port: 3000,
        pythonBridgeSharedSecret: "secret",
    logLevel: "info",
  };
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
