/**
 * Gateway contract tests for Discord-backed local-community outbound fanout.
 *
 * The local-community mode must deliver one user-authored Create activity to
 * each accepted remote follower inbox, not to the bridge's own local community
 * inbox. These checks keep the gateway-side fanout contract explicit.
 */

import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import initSqlJs from "sql.js";

import { publishLocalCommunityContent } from "../src/federation-outbound.js";
import type { GatewayConfig } from "../src/config.js";
import type { PublishLocalCommunityContentRequest } from "../src/types.js";

const TEST_ORIGIN = "https://discord-bridge.example.com/";

async function main(): Promise<void> {
  await testLocalCommunityPublishTargetsAcceptedFollowerInboxes();
  console.log("verify:local-community-publish passed");
}

/**
 * Action: a Discord-backed local community publishes one post while two
 * accepted remote followers are present.
 *
 * Expected: the gateway delivers to both remote follower inbox URLs and keeps
 * the activity audience pointed at the local community actor URL.
 */
async function testLocalCommunityPublishTargetsAcceptedFollowerInboxes(): Promise<void> {
  const sqlJs = await initSqlJs();
  const tempDir = await mkdtemp(path.join(tmpdir(), "fedify-local-community-publish-"));
  const databasePath = path.join(tempDir, "bridge.db");
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
        `${TEST_ORIGIN}communities/hackers`,
        `${TEST_ORIGIN}communities/hackers/inbox`,
        `${TEST_ORIGIN}communities/hackers/outbox`,
        `${TEST_ORIGIN}communities/hackers/followers`,
        "public-key",
        "private-key",
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
          status
        ) VALUES
          (1, 'https://lemmy.example/u/alice', 'https://lemmy.example/u/alice/inbox', 'https://lemmy.example/activities/follow/1', 'accepted'),
          (1, 'https://lemmy.example/u/bob', 'https://lemmy.example/u/bob/inbox', 'https://lemmy.example/activities/follow/2', 'accepted')
      `,
    );
    await writeFile(databasePath, Buffer.from(db.export()));
  } finally {
    db.close();
  }

  const config: GatewayConfig = {
    actorIdentifier: "bridge",
    actorName: "Bridge",
    actorSummary: "Bridge summary",
    bridgePrivateKeyJwkJson: null,
    bridgePublicKeyJwkJson: null,
    communityActorId: null,
    databaseUrl: `sqlite:///${databasePath}`,
    fedifyOrigin: TEST_ORIGIN,
    port: 3000,
    pythonBridgeEventsUrl: "http://127.0.0.1:8080/internal/activitypub/events",
    pythonBridgeSharedSecret: "secret",
    logLevel: "info",
  };

  const deliveries: Array<{ id: string; inboxId: string; payload: unknown }> = [];
  const fakeFederation = {
    createContext() {
      return {
        async sendActivity(
          _sender: unknown,
          recipient: { id: URL; inboxId: URL },
          activity: { toJsonLd(): Promise<unknown> },
        ): Promise<void> {
          deliveries.push({
            id: recipient.id.href,
            inboxId: recipient.inboxId.href,
            payload: await activity.toJsonLd(),
          });
        },
      };
    },
  };

  const request: PublishLocalCommunityContentRequest = {
    actorUsername: "alice",
    communityActorUrl: `${TEST_ORIGIN}communities/hackers`,
    kind: "post",
    title: "Hello from Discord",
    bodyMarkdown: "Body from Discord",
    inReplyToObjectId: null,
  };

  const result = await publishLocalCommunityContent(
    fakeFederation as never,
    config,
    request,
  );

  assert.equal(result.deliveredFollowerCount, 2);
  assert.equal(result.failedFollowerCount, 0);
  assert.deepEqual(
    deliveries.map((delivery) => delivery.inboxId).sort(),
    [
      "https://lemmy.example/u/alice/inbox",
      "https://lemmy.example/u/bob/inbox",
    ],
  );
  assert.ok(
    deliveries.every((delivery) => {
      const payload = delivery.payload as { object?: { audience?: string } };
      return payload.object?.audience === `${TEST_ORIGIN}communities/hackers`;
    }),
    "Each delivered object must stay addressed to the local community actor",
  );
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
