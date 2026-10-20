/**
 * Gateway contract tests for signed local-community relay delivery.
 *
 * Python renders the exact ActivityPub activity and chooses the explicit target
 * inboxes. The gateway must only sign as the local community actor, deliver to
 * those inboxes, and return per-target outcomes.
 */

import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { webcrypto } from "node:crypto";

import { exportJwk } from "@fedify/fedify";
import initSqlJs, { seedBridgeActorJwk } from "./support/sqlite-fixture.js";
import { closeAllPythonBridgeFixtures, importFixedRsaKeyPair, startPythonBridgeFixture } from "./support/python-bridge-fixture.js";
import { installSignedJsonSocketFixture, lowercaseSignedHeaders } from "./support/signed-json-socket-fixture.js";

import { sendLocalCommunityRelay } from "../src/federation-outbound.js";
import type { GatewayConfig } from "../src/config.js";
import type { SendLocalCommunityRelayRequest } from "../src/types.js";

const TEST_ORIGIN = "https://discord-bridge.example.com/";

async function main(): Promise<void> {
  await testRelaySendsAlreadyRenderedActivityToExplicitTargets();
  await testRelayRejectsMismatchedActorWithoutDelivery();
  console.log("verify:local-community-relay passed");
}

/**
 * Action: Python asks the gateway to relay an already-rendered Announce to two
 * explicit targets.
 *
 * Expected: the gateway sends the exact activity JSON to both inboxes and
 * reports one successful outcome per target.
 */
async function testRelaySendsAlreadyRenderedActivityToExplicitTargets(): Promise<void> {
  const deliveries: Array<{ inboxId: string; payload: unknown; headers: Record<string, string> }> = [];
  const restoreFetch = installFetchRecorder(deliveries);
  const config = await buildConfig();
  const activityJson = buildAnnounceJson();
  const request: SendLocalCommunityRelayRequest = {
    signingActorUrl: `${TEST_ORIGIN}actors/communities/hackers`,
    deliveries: [
      {
        deliveryId: 1,
        targetRemoteActorId: "https://lemmy.example/u/alice",
        targetInboxUrl: "https://lemmy.example/u/alice/inbox",
        activityJson,
      },
      {
        deliveryId: 2,
        targetRemoteActorId: "https://lemmy.example/u/carol",
        targetInboxUrl: "https://lemmy.example/u/carol/inbox",
        activityJson,
      },
    ],
  };

  const result = await sendLocalCommunityRelay({} as never, config, request);
  restoreFetch();

  assert.deepEqual(result.outcomes.map((outcome) => outcome.ok), [true, true]);
  assert.deepEqual(
    deliveries.map((delivery) => delivery.inboxId).sort(),
    ["https://lemmy.example/u/alice/inbox", "https://lemmy.example/u/carol/inbox"],
  );
  assert.ok(deliveries.every((delivery) => JSON.stringify(delivery.payload) === JSON.stringify(activityJson)));
  assert.ok(deliveries.every((delivery) => delivery.headers["content-type"] === "application/activity+json"));
  assert.ok(deliveries.every((delivery) => delivery.headers.signature.includes(`keyId="${TEST_ORIGIN}actors/communities/hackers#main-key"`)));
}

/**
 * Action: Python accidentally passes an activity whose actor does not match the
 * requested signing actor.
 *
 * Expected: the gateway refuses that target before attempting delivery.
 */
async function testRelayRejectsMismatchedActorWithoutDelivery(): Promise<void> {
  const deliveries: unknown[] = [];
  const restoreFetch = installFetchRecorder(deliveries as Array<{ inboxId: string; payload: unknown; headers: Record<string, string> }>);
  const activityJson = buildAnnounceJson();
  activityJson.actor = `${TEST_ORIGIN}communities/other`;

  const result = await sendLocalCommunityRelay({} as never, await buildConfig(), {
    signingActorUrl: `${TEST_ORIGIN}actors/communities/hackers`,
    deliveries: [
      {
        deliveryId: 3,
        targetRemoteActorId: "https://lemmy.example/u/alice",
        targetInboxUrl: "https://lemmy.example/u/alice/inbox",
        activityJson,
      },
    ],
  });
  restoreFetch();

  assert.equal(result.outcomes.length, 1);
  assert.equal(result.outcomes[0]?.ok, false);
  assert.equal(deliveries.length, 0);
}

/** Record signed relay requests at the transport boundary. */
function installFetchRecorder(
  deliveries: Array<{ inboxId: string; payload: unknown; headers: Record<string, string> }>,
): () => void {
  /** Capture signed relay delivery at its native HTTPS boundary. */
  return installSignedJsonSocketFixture((request) => {
    deliveries.push({
      inboxId: request.url,
      payload: JSON.parse(request.body) as Record<string, unknown>,
      headers: lowercaseSignedHeaders(request.headers),
    });
    return { status: 202 };
  });
}

async function buildConfig(): Promise<GatewayConfig> {
  /** Build a real local-community database so the relay helper can load the canonical signing key. */
  const sqlJs = await initSqlJs();
  const tempDir = await mkdtemp(path.join(tmpdir(), "fedify-local-community-relay-"));
  const databasePath = path.join(tempDir, "bridge.db");
  let pythonBridgeInternalUrl = "";
  const bridgeKeys = await importFixedRsaKeyPair();
  const communityKeys = await importFixedRsaKeyPair();
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
        await exportPublicKeyPem(communityKeys.publicKey),
        await exportPrivateKeyPem(communityKeys.privateKey),
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


function buildAnnounceJson(): Record<string, unknown> {
  /** Build the already-rendered relay activity that Python would submit. */
  return {
    "@context": "https://www.w3.org/ns/activitystreams",
    id: `${TEST_ORIGIN}actors/communities/hackers/activities/announce/1`,
    type: "Announce",
    actor: `${TEST_ORIGIN}actors/communities/hackers`,
    object: {
      type: "Create",
      id: "https://lemmy.example/activities/create/post/1",
      actor: "https://lemmy.example/u/bob",
      object: {
        type: "Page",
        id: "https://lemmy.example/post/1",
        attributedTo: "https://lemmy.example/u/bob",
      },
    },
  };
}

try {
  await main();
} catch (error) {
  console.error(error);
  process.exitCode = 1;
} finally {
  await closeAllPythonBridgeFixtures();
}
