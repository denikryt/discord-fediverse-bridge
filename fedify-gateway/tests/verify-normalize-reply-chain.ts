/**
 * Regression tests for resolvePostApIdFromJson with cross-domain reply chains.
 *
 * Bug: when a Lemmy user replies to a comment published from our gateway
 * (bot-test.nachitima.com), inReplyTo points to our domain — not a Lemmy path.
 * The old code used isLemmyPath() to identify posts, so it returned null and
 * the comment was dropped with "Could not resolve post AP ID".
 *
 * Fix: fetch the parent object and check type === "Page" (post) or "Note"
 * (comment), regardless of domain.
 *
 * System state:
 *   - A fake HTTP server hosts gateway AP objects (post and comment).
 *   - Objects reference each other with correct self-URLs.
 *
 * Tested actions:
 *   1. Lemmy comment with inReplyTo = gateway post URL
 *      -> post_ap_id resolves to the gateway post.
 *   2. Lemmy comment with inReplyTo = gateway comment URL (nested reply)
 *      -> post_ap_id walks up to the gateway post; parent_ap_id is the comment.
 */

import assert from "node:assert/strict";
import { rmSync } from "node:fs";
import { mkdtemp, writeFile } from "node:fs/promises";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";

import { Create, Note, Person, Source } from "@fedify/vocab";
import initSqlJs from "sql.js";

import {
  normalizeCreateActivity,
  normalizeCreateActivityFromJson,
} from "../src/normalize.js";

const LEMMY_ORIGIN = "https://lemmy.example";
const COMMUNITY_URL = `${LEMMY_ORIGIN}/c/testcommunity`;

async function main(): Promise<void> {
  await testLemmyReplyToGatewayPost();
  await testLemmyReplyToGatewayComment();
  await testDirectNoteReplyToMappedLocalComment();
  await testDirectNoteReplyToUnmappedLocalCommentRejects();
  console.log("normalize reply-chain regression tests passed");
}

/**
 * Starts a fake HTTP server that dynamically resolves gateway AP objects.
 * Handlers receive the server's own base URL so objects can self-reference.
 */
async function withFakeGateway(
  fn: (baseUrl: string) => Promise<void>,
): Promise<void> {
  let baseUrl!: string;

  const server = createServer((req: IncomingMessage, res: ServerResponse) => {
    const path = req.url ?? "/";

    if (path === "/users/alice/post/100") {
      res.writeHead(200, { "Content-Type": "application/activity+json" });
      res.end(JSON.stringify({
        "@context": "https://www.w3.org/ns/activitystreams",
        id: `${baseUrl}/users/alice/post/100`,
        type: "Page",
        attributedTo: `${baseUrl}/users/alice`,
        name: "Original post",
        to: ["as:Public", COMMUNITY_URL],
        audience: COMMUNITY_URL,
      }));
      return;
    }

    if (path === "/users/alice/comment/200") {
      res.writeHead(200, { "Content-Type": "application/activity+json" });
      res.end(JSON.stringify({
        "@context": "https://www.w3.org/ns/activitystreams",
        id: `${baseUrl}/users/alice/comment/200`,
        type: "Note",
        attributedTo: `${baseUrl}/users/alice`,
        inReplyTo: `${baseUrl}/users/alice/post/100`,
        to: [COMMUNITY_URL],
        audience: COMMUNITY_URL,
      }));
      return;
    }

    res.writeHead(404);
    res.end();
  });

  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
  const addr = server.address();
  if (addr == null || typeof addr === "string") throw new Error("no address");
  baseUrl = `http://127.0.0.1:${addr.port}`;

  try {
    await fn(baseUrl);
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((err) => (err ? reject(err) : resolve())),
    );
  }
}

/**
 * Action: Lemmy user replies to a post that was originally published from
 * the gateway (inReplyTo points to gateway domain, not lemmy domain).
 *
 * Expected: normalizeCreateActivityFromJson resolves post_ap_id to the gateway
 * post URL by fetching it and checking type === "Page".
 */
async function testLemmyReplyToGatewayPost(): Promise<void> {
  await withFakeGateway(async (gatewayBase) => {
    const gatewayPostUrl = `${gatewayBase}/users/alice/post/100`;

    const createActivity = {
      type: "Create",
      id: `${LEMMY_ORIGIN}/activities/create/direct-reply`,
      actor: `${LEMMY_ORIGIN}/u/bob`,
      to: ["as:Public", COMMUNITY_URL],
      object: {
        type: "Note",
        id: `${LEMMY_ORIGIN}/comment/50`,
        attributedTo: `${LEMMY_ORIGIN}/u/bob`,
        to: [COMMUNITY_URL],
        audience: COMMUNITY_URL,
        content: "<p>reply from lemmy to gateway post</p>",
        inReplyTo: gatewayPostUrl,
        published: "2026-05-08T12:00:00Z",
      },
    };

    const event = await normalizeCreateActivityFromJson(createActivity);

    assert.ok(event, "event must be normalized when inReplyTo is a gateway post URL");
    assert.equal(event.event_type, "comment.created");
    assert.equal(
      event.object.post_ap_id,
      gatewayPostUrl,
      "post_ap_id must resolve to gateway post even when inReplyTo is not a Lemmy path",
    );
    assert.equal(
      event.object.parent_ap_id,
      null,
      "parent_ap_id must be null when replying directly to a post",
    );
  });
}

/**
 * Action: Lemmy user replies to a gateway comment (two-level chain).
 * The gateway comment itself has inReplyTo pointing to the gateway post.
 *
 * Expected: resolvePostApIdFromJson walks:
 *   lemmy comment -> gateway comment (Note) -> gateway post (Page)
 * post_ap_id resolves to the gateway post; parent_ap_id is the gateway comment.
 */
async function testLemmyReplyToGatewayComment(): Promise<void> {
  await withFakeGateway(async (gatewayBase) => {
    const gatewayPostUrl = `${gatewayBase}/users/alice/post/100`;
    const gatewayCommentUrl = `${gatewayBase}/users/alice/comment/200`;

    const createActivity = {
      type: "Create",
      id: `${LEMMY_ORIGIN}/activities/create/nested-reply`,
      actor: `${LEMMY_ORIGIN}/u/carol`,
      to: ["as:Public", COMMUNITY_URL],
      object: {
        type: "Note",
        id: `${LEMMY_ORIGIN}/comment/51`,
        attributedTo: `${LEMMY_ORIGIN}/u/carol`,
        to: [COMMUNITY_URL],
        audience: COMMUNITY_URL,
        content: "<p>nested reply to gateway comment</p>",
        inReplyTo: gatewayCommentUrl,
        published: "2026-05-08T12:01:00Z",
      },
    };

    const event = await normalizeCreateActivityFromJson(createActivity);

    assert.ok(event, "event must be normalized when inReplyTo is a gateway comment URL");
    assert.equal(event.event_type, "comment.created");
    assert.equal(
      event.object.post_ap_id,
      gatewayPostUrl,
      "post_ap_id must walk up the gateway comment chain to find the gateway post",
    );
    assert.equal(
      event.object.parent_ap_id,
      gatewayCommentUrl,
      "parent_ap_id must be the direct gateway comment parent",
    );
  });
}

/**
 * Action: a Mastodon-shaped server sends a direct Create(Note) reply to a
 * gateway-owned comment. The Note does not address the local community; its
 * inReplyTo parent mapping is the only safe routing anchor.
 *
 * Expected: normalization emits comment.created, derives community_actor_id from
 * message_mappings, and accepts the non-Lemmy status URL with lemmy_id = 0.
 */
async function testDirectNoteReplyToMappedLocalComment(): Promise<void> {
  const previousDatabaseUrl = process.env.DATABASE_URL;
  const tempDir = await mkdtemp(path.join(tmpdir(), "fedify-direct-note-parent-"));
  const databasePath = path.join(tempDir, "bridge.db");
  const databaseUrl = `sqlite:///${databasePath}`;

  try {
    await writeBridgeParentDatabase(databasePath);
    process.env.DATABASE_URL = databaseUrl;

    const activity = new Create({
      id: new URL("https://mastodon.example/users/alice/statuses/900/activity"),
      actor: new Person({
        id: new URL("https://mastodon.example/users/alice"),
        preferredUsername: "alice",
      }),
      object: new Note({
        id: new URL("https://mastodon.example/users/alice/statuses/900"),
        replyTarget: new URL("https://bridge.example/users/bob/comment/200"),
        source: new Source({
          content: "reply from mastodon-shaped server",
          mediaType: "text/markdown",
        }),
        url: new URL("https://mastodon.example/@alice/900"),
      }),
    });

    const event = await normalizeCreateActivity(activity);

    assert.ok(event, "direct Note reply to mapped local parent must normalize");
    assert.equal(event.event_type, "comment.created");
    assert.equal(event.community_actor_id, "https://bridge.example/communities/general");
    assert.equal(event.object.lemmy_id, 0);
    assert.equal(event.object.parent_ap_id, "https://bridge.example/users/bob/comment/200");
    assert.equal(event.object.post_ap_id, "https://bridge.example/users/bob/post/100");
    assert.equal(event.object.post_lemmy_id, 0);
  } finally {
    if (previousDatabaseUrl == null) {
      delete process.env.DATABASE_URL;
    } else {
      process.env.DATABASE_URL = previousDatabaseUrl;
    }
    rmSync(tempDir, { force: true, recursive: true });
  }
}


/**
 * Action: a direct Note reply targets a local object that is fetchable but has
 * no message_mappings row.
 *
 * Expected: normalization rejects the reply because fetchability alone does not
 * define Discord placement.
 */
async function testDirectNoteReplyToUnmappedLocalCommentRejects(): Promise<void> {
  const previousDatabaseUrl = process.env.DATABASE_URL;
  const tempDir = await mkdtemp(path.join(tmpdir(), "fedify-direct-note-unmapped-"));
  const databasePath = path.join(tempDir, "bridge.db");

  try {
    await writeBridgeParentDatabase(databasePath, { includeMapping: false });
    process.env.DATABASE_URL = `sqlite:///${databasePath}`;

    const activity = new Create({
      id: new URL("https://mastodon.example/users/alice/statuses/901/activity"),
      actor: new Person({
        id: new URL("https://mastodon.example/users/alice"),
        preferredUsername: "alice",
      }),
      object: new Note({
        id: new URL("https://mastodon.example/users/alice/statuses/901"),
        replyTarget: new URL("https://bridge.example/users/bob/comment/200"),
        source: new Source({
          content: "reply with no placement mapping",
          mediaType: "text/markdown",
        }),
      }),
    });

    await assert.rejects(
      () => normalizeCreateActivity(activity),
      /Could not resolve community actor id for comment reply parent/,
    );
  } finally {
    if (previousDatabaseUrl == null) {
      delete process.env.DATABASE_URL;
    } else {
      process.env.DATABASE_URL = previousDatabaseUrl;
    }
    rmSync(tempDir, { force: true, recursive: true });
  }
}

/**
 * Builds the minimal bridge DB state required for local-parent community
 * resolution. message_mappings is intentionally present because Discord
 * placement must not fall back to fetchability-only published objects.
 */
async function writeBridgeParentDatabase(
  databasePath: string,
  options: { includeMapping?: boolean } = {},
): Promise<void> {
  const sqlJs = await initSqlJs();
  const db = new sqlJs.Database();
  try {
    db.run(`
      CREATE TABLE published_activity_objects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor_username TEXT NOT NULL,
        actor_url TEXT NOT NULL,
        community_actor_url TEXT NOT NULL,
        activity_id TEXT NOT NULL UNIQUE,
        object_id TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL,
        title TEXT NULL,
        body_markdown TEXT NOT NULL,
        in_reply_to_object_id TEXT NULL,
        discord_channel_id INTEGER NULL,
        discord_message_id INTEGER NULL,
        published_at TEXT NOT NULL,
        created_at TEXT NOT NULL
      )
    `);
    db.run(`
      CREATE TABLE message_mappings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_platform TEXT NOT NULL,
        source_id TEXT NOT NULL,
        activity_id TEXT NOT NULL UNIQUE,
        object_id TEXT NOT NULL UNIQUE,
        actor_url TEXT NOT NULL,
        community_actor_url TEXT NOT NULL,
        discord_channel_id INTEGER NULL,
        discord_message_id INTEGER NULL,
        created_at TEXT NOT NULL
      )
    `);
    db.run(
      `
        INSERT INTO published_activity_objects (
          actor_username, actor_url, community_actor_url, activity_id, object_id,
          kind, title, body_markdown, in_reply_to_object_id,
          discord_channel_id, discord_message_id, published_at, created_at
        ) VALUES
          (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?),
          (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `,
      [
        "bob",
        "https://bridge.example/actors/bob",
        "https://bridge.example/communities/general",
        "https://bridge.example/users/bob/activities/create/post/100",
        "https://bridge.example/users/bob/post/100",
        "post",
        "Root post",
        "root body",
        null,
        10,
        1000,
        "2026-05-08T12:00:00Z",
        "2026-05-08T12:00:00Z",
        "bob",
        "https://bridge.example/actors/bob",
        "https://bridge.example/communities/general",
        "https://bridge.example/users/bob/activities/create/comment/200",
        "https://bridge.example/users/bob/comment/200",
        "comment",
        null,
        "parent comment body",
        "https://bridge.example/users/bob/post/100",
        10,
        2000,
        "2026-05-08T12:01:00Z",
        "2026-05-08T12:01:00Z",
      ],
    );
    if (options.includeMapping !== false) {
      db.run(
        `
          INSERT INTO message_mappings (
            source_platform, source_id, activity_id, object_id, actor_url,
            community_actor_url, discord_channel_id, discord_message_id, created_at
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        `,
        [
          "discord",
          "2000",
          "https://bridge.example/users/bob/activities/create/comment/200",
          "https://bridge.example/users/bob/comment/200",
          "https://bridge.example/actors/bob",
          "https://bridge.example/communities/general",
          10,
          2000,
          "2026-05-08T12:01:00Z",
        ],
      );
    }
    await writeFile(databasePath, Buffer.from(db.export()));
  } finally {
    db.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
