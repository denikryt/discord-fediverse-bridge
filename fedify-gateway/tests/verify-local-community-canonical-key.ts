/**
 * Gateway verification for local-community canonical key identity.
 *
 * Local communities are canonical ActivityPub Group actors under
 * `/communities/{slug}`. Fedify's generic actor dispatcher lives under
 * `/actors/{identifier}`, so this test locks the compatibility invariant that
 * community actor documents and outbound community signatures use the
 * community URL as the key owner/key id.
 */
import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { webcrypto } from "node:crypto";

import { exportJwk, generateCryptoKeyPair } from "@fedify/fedify";
import initSqlJs from "sql.js";

import { buildLocalCommunityGroupActor } from "../src/actors.js";
import { loadLocalCommunityIdentity } from "../src/actor-store.js";
import { createGatewayApp } from "../src/server.js";
import { acceptLocalCommunityFollow, sendLocalCommunityRelay } from "../src/federation-outbound.js";
import { buildLocalCommunityPublicKeyCarrier } from "../src/local-community-keys.js";
import type { GatewayConfig } from "../src/config.js";
import type { SendLocalCommunityRelayRequest } from "../src/types.js";

const TEST_ORIGIN = "https://discord-bridge.example.com/";
const REMOTE_ORIGIN = "https://mastodon.example/";

async function main(): Promise<void> {
  const config = await buildConfig();
  await testActorBuilderPublishesCanonicalKey(config);
  await testHttpActorRoutesPublishCanonicalKey(config);
  await testAcceptFollowSignsWithCanonicalCommunityKey(config);
  await testRelaySignsWithCanonicalCommunityKey(config);
  console.log("verify:local-community-canonical-key passed");
}

/**
 * Action: build the local-community Group actor from the DB-backed identity.
 * Expected: actor id, public key id, and key owner all use /communities/hackers.
 */
async function testActorBuilderPublishesCanonicalKey(config: GatewayConfig): Promise<void> {
  const identity = await loadLocalCommunityIdentity(config, "hackers");
  assert.ok(identity != null);

  const actor = buildLocalCommunityGroupActor(
    identity,
    new URL("/inbox", config.fedifyOrigin),
    await buildLocalCommunityPublicKeyCarrier(identity),
  );
  const json = await actor.toJsonLd() as Record<string, unknown>;
  const publicKey = json.publicKey as Record<string, unknown>;

  assert.equal(json.id, `${TEST_ORIGIN}communities/hackers`);
  assert.equal(publicKey.id, `${TEST_ORIGIN}communities/hackers#main-key`);
  assert.equal(publicKey.owner, `${TEST_ORIGIN}communities/hackers`);
}

/**
 * Action: fetch the community through both public routes.
 * Expected: every route returns the same canonical actor/key ownership.
 */
async function testHttpActorRoutesPublishCanonicalKey(config: GatewayConfig): Promise<void> {
  const app = createGatewayApp(config);

  for (const pathName of ["/communities/hackers", "/actors/hackers"]) {
    const response = await app.request(pathName, {
      headers: { Accept: "application/activity+json" },
    });
    assert.equal(response.status, 200, `${pathName} should resolve`);
    const json = await response.json() as Record<string, unknown>;
    const publicKey = json.publicKey as Record<string, unknown>;

    assert.equal(json.id, `${TEST_ORIGIN}communities/hackers`);
    assert.equal(publicKey.id, `${TEST_ORIGIN}communities/hackers#main-key`);
    assert.equal(publicKey.owner, `${TEST_ORIGIN}communities/hackers`);
  }
}

/**
 * Action: Python asks the gateway to Accept a Mastodon-style Follow.
 * Expected: the Accept is sent with the canonical community key id.
 */
async function testAcceptFollowSignsWithCanonicalCommunityKey(config: GatewayConfig): Promise<void> {
  const deliveries: Array<{ sender: unknown; recipient: string; payload: Record<string, unknown> }> = [];
  const fakeFederation = buildFakeFederation(deliveries);

  await acceptLocalCommunityFollow(fakeFederation as never, config, {
    communitySlug: "hackers",
    communityActorUrl: `${TEST_ORIGIN}communities/hackers`,
    remoteActorId: `${REMOTE_ORIGIN}ap/users/alice`,
    remoteInboxUrl: `${REMOTE_ORIGIN}ap/users/alice/inbox`,
    followActivityId: `${REMOTE_ORIGIN}activities/follow/1`,
  });

  assert.equal(deliveries.length, 1);
  assertSenderKey(deliveries[0]?.sender);
  assert.equal(deliveries[0]?.recipient, `${REMOTE_ORIGIN}ap/users/alice/inbox`);
  assert.equal(deliveries[0]?.payload.type, "Accept");
  assert.equal(deliveries[0]?.payload.actor, `${TEST_ORIGIN}communities/hackers`);
  assert.equal(deliveries[0]?.payload.to, `${REMOTE_ORIGIN}ap/users/alice`);
  assert.equal((deliveries[0]?.payload.object as Record<string, unknown>).id, `${REMOTE_ORIGIN}activities/follow/1`);
  assert.equal((deliveries[0]?.payload.object as Record<string, unknown>).actor, `${REMOTE_ORIGIN}ap/users/alice`);
  assert.equal((deliveries[0]?.payload.object as Record<string, unknown>).object, `${TEST_ORIGIN}communities/hackers`);
}

/**
 * Action: Python asks the gateway to relay an already-rendered Announce.
 * Expected: the gateway does not rewrite the activity and signs with the
 * canonical community key id.
 */
async function testRelaySignsWithCanonicalCommunityKey(config: GatewayConfig): Promise<void> {
  const deliveries: Array<{ inboxId: string; payload: Record<string, unknown>; headers: Record<string, string> }> = [];
  const restoreFetch = installFetchRecorder(deliveries);
  const activityJson = {
    "@context": "https://www.w3.org/ns/activitystreams",
    id: `${TEST_ORIGIN}communities/hackers/activities/announce/1`,
    type: "Announce",
    actor: `${TEST_ORIGIN}communities/hackers`,
    object: {
      type: "Create",
      actor: `${REMOTE_ORIGIN}ap/users/bob`,
      object: `${REMOTE_ORIGIN}objects/1`,
    },
  };
  const request: SendLocalCommunityRelayRequest = {
    signingActorUrl: `${TEST_ORIGIN}communities/hackers`,
    deliveries: [
      {
        deliveryId: 10,
        targetRemoteActorId: `${REMOTE_ORIGIN}ap/users/alice`,
        targetInboxUrl: `${REMOTE_ORIGIN}ap/users/alice/inbox`,
        activityJson,
      },
    ],
  };

  const result = await sendLocalCommunityRelay({} as never, config, request);
  restoreFetch();

  assert.equal(result.outcomes[0]?.ok, true);
  assert.equal(deliveries.length, 1);
  assert.equal(deliveries[0]?.payload.id, activityJson.id);
  assert.equal(deliveries[0]?.payload.type, activityJson.type);
  assert.equal(deliveries[0]?.headers["content-type"], "application/activity+json");
  assert.match(deliveries[0]?.headers.signature ?? "", /keyId="https:\/\/discord-bridge.example.com\/communities\/hackers#main-key"/);
}

function installFetchRecorder(
  deliveries: Array<{ inboxId: string; payload: Record<string, unknown>; headers: Record<string, string> }>,
): () => void {
  /** Capture signed JSON relay delivery so key identity is verified at the HTTP signature boundary. */
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const headers = new Headers(init?.headers);
    deliveries.push({
      inboxId: input instanceof URL ? input.href : String(input),
      payload: JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>,
      headers: Object.fromEntries(headers.entries()),
    });
    return new Response("", { status: 202, statusText: "Accepted" });
  };
  return () => {
    globalThis.fetch = originalFetch;
  };
}

function buildFakeFederation(
  deliveries: Array<{ sender: unknown; recipient: string; payload: Record<string, unknown> }>,
): unknown {
  /** Build the minimal Fedify context seam that records outbound delivery. */
  return {
    createContext() {
      return {
        async sendActivity(
          sender: unknown,
          recipient: { inboxId: URL },
          activity: { toJsonLd(): Promise<Record<string, unknown>> },
        ): Promise<void> {
          deliveries.push({
            sender,
            recipient: recipient.inboxId.href,
            payload: await activity.toJsonLd(),
          });
        },
      };
    },
  };
}

function assertSenderKey(sender: unknown): void {
  /** Assert that local-community outbound delivery uses the canonical key id. */
  const keyPairs = sender as Array<{ keyId: URL; privateKey: CryptoKey }>;
  assert.equal(keyPairs[0]?.keyId.href, `${TEST_ORIGIN}communities/hackers#main-key`);
  assert.ok(keyPairs[0]?.privateKey != null);
}

async function buildConfig(): Promise<GatewayConfig> {
  /** Create a temporary Python-style database containing one local community. */
  const sqlJs = await initSqlJs();
  const tempDir = await mkdtemp(path.join(tmpdir(), "fedify-canonical-community-key-"));
  const databasePath = path.join(tempDir, "bridge.db");
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

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
