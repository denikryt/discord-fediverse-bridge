/**
 * Gateway contract tests for Lemmy-compatible local-community publish fanout.
 *
 * Local communities are ActivityPub Group actors. When Discord-authored content
 * is published into a local community, the gateway must preserve the user-owned
 * Create(Page|Note) as canonical content and fan it out as a community-owned
 * Announce wrapper, matching Lemmy's community federation shape.
 */

import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { exportJwk, generateCryptoKeyPair } from "@fedify/fedify";
import initSqlJs from "sql.js";

import { publishLocalCommunityContent } from "../src/federation-outbound.js";
import type { GatewayConfig } from "../src/config.js";
import type { PublishLocalCommunityContentRequest } from "../src/types.js";

const TEST_ORIGIN = "https://discord-bridge.example.com/";
const COMMUNITY_ACTOR = `${TEST_ORIGIN}communities/hackers`;
const COMMUNITY_FOLLOWERS = `${COMMUNITY_ACTOR}/followers`;
const USER_ACTOR = `${TEST_ORIGIN}actors/alice`;
const PUBLIC = "https://www.w3.org/ns/activitystreams#Public";

interface DeliveryRecord {
  inboxId: string;
  payload: Record<string, unknown>;
  headers: Record<string, string>;
}

async function main(): Promise<void> {
  await testLocalCommunityPostPublishesAnnounceCreatePage();
  await testLocalCommunityCommentPublishesAnnounceCreateNote();
  await testLocalCommunityPublishReportsPartialFailure();
  await testLogLevelDebugControlsSignedJsonDeliveryLogs();
  console.log("verify:local-community-publish passed");
}

/**
 * Action: a Discord-authored post is published into a local community.
 * Expected: each accepted follower receives a community Announce wrapping the
 * user-authored Create(Page), and Python-facing ids remain the embedded ids.
 */
async function testLocalCommunityPostPublishesAnnounceCreatePage(): Promise<void> {
  const config = await buildConfig();
  const deliveries: DeliveryRecord[] = [];
  const restoreFetch = installFetchRecorder(deliveries);

  const result = await publishLocalCommunityContent(
    {} as never,
    config,
    {
      actorUsername: "alice",
      communityActorUrl: COMMUNITY_ACTOR,
      kind: "post",
      title: "Hello from Discord",
      bodyMarkdown: "Body from Discord",
      inReplyToObjectId: null,
    },
  );
  restoreFetch();

  assert.equal(result.deliveredFollowerCount, 2);
  assert.equal(result.failedFollowerCount, 0);
  assert.equal(result.communityActorUrl, COMMUNITY_ACTOR);
  assert.deepEqual(
    deliveries.map((delivery) => delivery.inboxId).sort(),
    [
      "https://lemmy.example/u/alice/inbox",
      "https://mastodon.example/ap/users/bob/inbox",
    ],
  );

  for (const delivery of deliveries) {
    assertSignedJsonDelivery(delivery);
    assert.equal(delivery.payload.type, "Announce");
    assert.equal(delivery.payload.actor, COMMUNITY_ACTOR);
    assert.ok(Array.isArray(delivery.payload.to));
    assert.ok(Array.isArray(delivery.payload.cc));
    assert.ok(toList(delivery.payload.to).includes(PUBLIC));
    assert.ok(toList(delivery.payload.cc).includes(COMMUNITY_FOLLOWERS));

    const embeddedCreate = delivery.payload.object as Record<string, unknown>;
    const embeddedObject = embeddedCreate.object as Record<string, unknown>;
    assert.equal(embeddedCreate.type, "Create");
    assert.equal(embeddedCreate.actor, USER_ACTOR);
    assert.equal(embeddedObject.type, "Page");
    assert.equal(embeddedObject.attributedTo, USER_ACTOR);
    assert.equal(embeddedObject.audience, COMMUNITY_ACTOR);
    assert.ok(Array.isArray(embeddedCreate.to));
    assert.ok(Array.isArray(embeddedObject.to));
    assert.ok(toList(embeddedCreate.to).includes(PUBLIC));
    assert.ok(toList(embeddedCreate.to).includes(COMMUNITY_ACTOR));
    assert.ok(toList(embeddedObject.to).includes(PUBLIC));
    assert.ok(toList(embeddedObject.to).includes(COMMUNITY_ACTOR));
    assert.ok(toList(embeddedObject.cc).includes(USER_ACTOR));
    assert.notEqual(delivery.payload.actor, embeddedObject.attributedTo);
    assert.equal(result.activityId, embeddedCreate.id);
    assert.equal(result.objectId, embeddedObject.id);
    assert.notEqual(result.activityId, delivery.payload.id);
  }
}

/**
 * Action: a Discord-authored reply is published into a local community.
 * Expected: the community announces the user-owned Create(Note) without
 * rewriting author, object id, or inReplyTo mapping.
 */
async function testLocalCommunityCommentPublishesAnnounceCreateNote(): Promise<void> {
  const config = await buildConfig();
  const deliveries: DeliveryRecord[] = [];
  const restoreFetch = installFetchRecorder(deliveries);
  const parentObjectId = `${TEST_ORIGIN}users/alice/post/parent`;

  const result = await publishLocalCommunityContent(
    {} as never,
    config,
    {
      actorUsername: "alice",
      communityActorUrl: COMMUNITY_ACTOR,
      kind: "comment",
      title: null,
      bodyMarkdown: "Reply from Discord",
      inReplyToObjectId: parentObjectId,
    },
  );

  assert.equal(deliveries.length, 2);
  for (const delivery of deliveries) {
    assertSignedJsonDelivery(delivery);
    assert.equal(delivery.payload.type, "Announce");
    assert.equal(delivery.payload.actor, COMMUNITY_ACTOR);

    const embeddedCreate = delivery.payload.object as Record<string, unknown>;
    const embeddedObject = embeddedCreate.object as Record<string, unknown>;
    assert.equal(embeddedCreate.type, "Create");
    assert.equal(embeddedCreate.actor, USER_ACTOR);
    assert.equal(embeddedObject.type, "Note");
    assert.equal(embeddedObject.attributedTo, USER_ACTOR);
    assert.equal(embeddedObject.audience, COMMUNITY_ACTOR);
    assert.ok(toList(embeddedObject.inReplyTo).includes(parentObjectId));
    assert.equal(result.activityId, embeddedCreate.id);
    assert.equal(result.objectId, embeddedObject.id);
  }
}

/**
 * Action: one accepted follower inbox rejects the announced activity.
 * Expected: fanout continues to healthy followers and reports per-target counts.
 */
async function testLocalCommunityPublishReportsPartialFailure(): Promise<void> {
  const config = await buildConfig();
  const deliveries: DeliveryRecord[] = [];
  const restoreFetch = installFetchRecorder(
    deliveries,
    new Set(["https://mastodon.example/ap/users/bob/inbox"]),
  );

  const result = await publishLocalCommunityContent(
    {} as never,
    config,
    {
      actorUsername: "alice",
      communityActorUrl: COMMUNITY_ACTOR,
      kind: "post",
      title: "Partial failure",
      bodyMarkdown: "Healthy followers should still receive this.",
      inReplyToObjectId: null,
    },
  );
  restoreFetch();

  assert.equal(result.deliveredFollowerCount, 1);
  assert.equal(result.failedFollowerCount, 1);
  assert.equal(deliveries.length, 1);
  assert.equal(deliveries[0]?.payload.type, "Announce");
  assert.equal(deliveries[0]?.inboxId, "https://lemmy.example/u/alice/inbox");
}

/**
 * Action: the same local-community publish runs with normal and debug log levels.
 * Expected: signed JSON delivery is always used, but verbose raw delivery logs
 * are controlled only by LOG_LEVEL=debug.
 */
async function testLogLevelDebugControlsSignedJsonDeliveryLogs(): Promise<void> {
  const originalConsoleLog = console.log;
  const logs: unknown[][] = [];

  try {
    console.log = (...args: unknown[]) => {
      logs.push(args);
    };

    const infoDeliveries: DeliveryRecord[] = [];
    const restoreInfoFetch = installFetchRecorder(infoDeliveries);
    await publishLocalCommunityContent({} as never, await buildConfig("info"), {
      actorUsername: "alice",
      communityActorUrl: COMMUNITY_ACTOR,
      kind: "post",
      title: "Info logging",
      bodyMarkdown: "Normal logging should not emit raw request bodies.",
      inReplyToObjectId: null,
    });
    restoreInfoFetch();

    assert.equal(infoDeliveries.length, 2);
    assert.equal(hasRawDeliveryLog(logs), false);

    logs.length = 0;
    const debugDeliveries: DeliveryRecord[] = [];
    const restoreDebugFetch = installFetchRecorder(debugDeliveries);
    await publishLocalCommunityContent({} as never, await buildConfig("debug"), {
      actorUsername: "alice",
      communityActorUrl: COMMUNITY_ACTOR,
      kind: "post",
      title: "Debug logging",
      bodyMarkdown: "LOG_LEVEL=debug should enable raw logs.",
      inReplyToObjectId: null,
    });
    restoreDebugFetch();

    assert.equal(debugDeliveries.length, 2);
    assert.equal(hasRawDeliveryLog(logs), true);
  } finally {
    console.log = originalConsoleLog;
  }
}

function installFetchRecorder(
  deliveries: DeliveryRecord[],
  failingInboxes: Set<string> = new Set(),
): () => void {
  /** Capture signed JSON HTTP delivery without relying on Fedify sendActivity. */
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const inboxId = input instanceof URL ? input.href : String(input);
    if (failingInboxes.has(inboxId)) {
      return new Response("simulated failure", { status: 500, statusText: "Internal Server Error" });
    }
    const headers = new Headers(init?.headers);
    deliveries.push({
      inboxId,
      payload: JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>,
      headers: Object.fromEntries(headers.entries()),
    });
    return new Response("", { status: 202, statusText: "Accepted" });
  };
  return () => {
    globalThis.fetch = originalFetch;
  };
}

async function buildConfig(logLevel: "info" | "debug" = "info"): Promise<GatewayConfig> {
  /** Create a temporary Python-style DB with one local community and followers. */
  const sqlJs = await initSqlJs();
  const tempDir = await mkdtemp(path.join(tmpdir(), "fedify-local-community-publish-"));
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
    db.run(`
      CREATE TABLE local_community_followers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        local_community_id INTEGER NOT NULL,
        remote_actor_id VARCHAR(512) NOT NULL,
        remote_inbox_url VARCHAR(512) NOT NULL,
        follow_activity_id VARCHAR(512) NOT NULL,
        status VARCHAR(32) NOT NULL,
        created_at VARCHAR(64)
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
        COMMUNITY_FOLLOWERS,
        await exportPublicKeyPem(communityKeys.publicKey),
        await exportPrivateKeyPem(communityKeys.privateKey),
        "active",
      ],
    );
    db.run(
      `
        INSERT INTO local_community_followers (
          local_community_id,
          remote_actor_id,
          remote_inbox_url,
          follow_activity_id,
          status,
          created_at
        ) VALUES
          (1, 'https://lemmy.example/u/alice', 'https://lemmy.example/u/alice/inbox', 'https://lemmy.example/activities/follow/1', 'accepted', '2026-01-01T00:00:00Z'),
          (1, 'https://mastodon.example/ap/users/bob', 'https://mastodon.example/ap/users/bob/inbox', 'https://mastodon.example/activities/follow/2', 'accepted', '2026-01-01T00:00:01Z')
      `,
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
    logLevel,
  };
}

function hasRawDeliveryLog(logs: unknown[][]): boolean {
  /** Check whether verbose signed JSON request logging was emitted. */
  return logs.some((entry) => String(entry[0]).includes("Raw ActivityPub request"));
}

function assertSignedJsonDelivery(delivery: DeliveryRecord): void {
  /** The exact JSON fanout path signs HTTP requests with the community key. */
  assert.equal(delivery.headers["content-type"], "application/activity+json");
  assert.match(delivery.headers.signature ?? "", /keyId="https:\/\/discord-bridge.example.com\/communities\/hackers#main-key"/);
  assert.match(delivery.headers.signature ?? "", /headers="\(request-target\) host date digest content-type"/);
  assert.ok(delivery.headers.digest?.startsWith("SHA-256="));
}

function toList(value: unknown): string[] {
  /** Normalize ActivityPub scalar-or-array addressing fields for assertions. */
  if (Array.isArray(value)) return value.map(String);
  if (value != null) return [String(value)];
  return [];
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
  /** Wrap DER bytes as PEM text for the Python-style SQLite test fixture. */
  const base64 = bytes.toString("base64");
  const wrapped = base64.match(/.{1,64}/g)?.join("\n") ?? base64;
  return `-----BEGIN ${label}-----\n${wrapped}\n-----END ${label}-----\n`;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
