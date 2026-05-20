import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import {
  loadPublishedActivityObjectByActivityIdForDatabaseUrl,
  loadPublishedActivityObjectByObjectIdForDatabaseUrl,
} from "../src/db.js";
import { normalizeCreateActivityFromJson } from "../src/normalize.js";
import { createGatewayApp } from "../src/server.js";
import {
  buildPublishedActivityObjectJson,
  buildPublishedCreateActivityJson,
} from "../src/published-objects.js";
import type { GatewayConfig } from "../src/config.js";

const BRIDGE_ORIGIN = "https://bot-test.example.com";
const LEMMY_ORIGIN = "https://lemmy.example";
const COMMUNITY_URL = `${LEMMY_ORIGIN}/c/testcommunity`;
const PUBLIC_IRI = "https://www.w3.org/ns/activitystreams#Public";
const CANONICAL_ALICE_ACTOR = `${BRIDGE_ORIGIN}/actors/alice`;

async function main(): Promise<void> {
  await testStoredPostBuildsPageJsonAndResolvesWithoutHttp();
  await testStoredCommentWalksLocalReplyChainWithoutHttp();
  await testStoredActivitiesLoadByActivityIdAndRenderCreate();
  await testStoredCreateActivityRoutesAreFetchable();
  await testActivityLookupHandlesUnknownAndMissingTable();
  await testStoredObjectsRemainReadableAcrossFreshDbOpens();
  console.log("published object store verification passed");
}

async function testStoredPostBuildsPageJsonAndResolvesWithoutHttp(): Promise<void> {
  await withPublishedObjectDatabase(
    [
      {
        actor_username: "alice",
        actor_url: `${BRIDGE_ORIGIN}/users/alice`,
        community_actor_url: COMMUNITY_URL,
        activity_id: `${BRIDGE_ORIGIN}/users/alice/activities/create/post/100`,
        object_id: `${BRIDGE_ORIGIN}/users/alice/post/100`,
        kind: "post",
        title: "Stored post",
        body_markdown: "hello from discord",
        in_reply_to_object_id: null,
      },
    ],
    async (databaseUrl) => {
      process.env.DATABASE_URL = databaseUrl;
      const storedPost = await loadPublishedActivityObjectByObjectIdForDatabaseUrl(
        databaseUrl,
        `${BRIDGE_ORIGIN}/users/alice/post/100`,
      );
      assert.ok(storedPost, "stored post must be readable from the shared DB");

      const objectJson = buildPublishedActivityObjectJson(storedPost);
      assert.equal(objectJson.type, "Page");
      assert.equal(objectJson.id, `${BRIDGE_ORIGIN}/users/alice/post/100`);
      assert.equal(objectJson.attributedTo, CANONICAL_ALICE_ACTOR);
      assert.deepEqual(objectJson.to, [PUBLIC_IRI, COMMUNITY_URL]);
      assert.deepEqual(objectJson.cc, [CANONICAL_ALICE_ACTOR]);
      assert.equal(objectJson.content, "<p>hello from discord</p>");
      assert.deepEqual(objectJson.source, {
        content: "hello from discord",
        mediaType: "text/markdown",
      });
      assert.equal(objectJson.published, "2026-05-08T12:00:00Z");

      const event = await normalizeCreateActivityFromJson({
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
          content: "<p>reply from lemmy to stored gateway post</p>",
          inReplyTo: `${BRIDGE_ORIGIN}/users/alice/post/100`,
          published: "2026-05-08T12:00:00Z",
        },
      });

      assert.ok(event, "DB-backed local post must resolve without any HTTP route");
      assert.equal(event.event_type, "comment.created");
      assert.equal(event.object.post_ap_id, `${BRIDGE_ORIGIN}/users/alice/post/100`);
      assert.equal(event.object.parent_ap_id, null);
      assert.equal(event.object.post_lemmy_id, 0);
    },
  );
}

async function testStoredCommentWalksLocalReplyChainWithoutHttp(): Promise<void> {
  await withPublishedObjectDatabase(
    [
      {
        actor_username: "alice",
        actor_url: `${BRIDGE_ORIGIN}/users/alice`,
        community_actor_url: COMMUNITY_URL,
        activity_id: `${BRIDGE_ORIGIN}/users/alice/activities/create/post/100`,
        object_id: `${BRIDGE_ORIGIN}/users/alice/post/100`,
        kind: "post",
        title: "Stored post",
        body_markdown: "hello from discord",
        in_reply_to_object_id: null,
      },
      {
        actor_username: "alice",
        actor_url: `${BRIDGE_ORIGIN}/users/alice`,
        community_actor_url: COMMUNITY_URL,
        activity_id: `${BRIDGE_ORIGIN}/users/alice/activities/create/comment/200`,
        object_id: `${BRIDGE_ORIGIN}/users/alice/comment/200`,
        kind: "comment",
        title: null,
        body_markdown: "first reply",
        in_reply_to_object_id: `${BRIDGE_ORIGIN}/users/alice/post/100`,
      },
    ],
    async (databaseUrl) => {
      process.env.DATABASE_URL = databaseUrl;
      const storedComment = await loadPublishedActivityObjectByObjectIdForDatabaseUrl(
        databaseUrl,
        `${BRIDGE_ORIGIN}/users/alice/comment/200`,
      );
      assert.ok(storedComment, "stored comment must be readable from the shared DB");

      const objectJson = buildPublishedActivityObjectJson(storedComment);
      assert.equal(objectJson.type, "Note");
      assert.equal(objectJson.attributedTo, CANONICAL_ALICE_ACTOR);
      assert.deepEqual(objectJson.to, [PUBLIC_IRI, COMMUNITY_URL]);
      assert.deepEqual(objectJson.cc, [CANONICAL_ALICE_ACTOR]);
      assert.equal(objectJson.content, "<p>first reply</p>");
      assert.equal(objectJson.published, "2026-05-08T12:00:00Z");
      assert.equal(
        objectJson.inReplyTo,
        `${BRIDGE_ORIGIN}/users/alice/post/100`,
      );

      const event = await normalizeCreateActivityFromJson({
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
          content: "<p>nested reply to stored gateway comment</p>",
          inReplyTo: `${BRIDGE_ORIGIN}/users/alice/comment/200`,
          published: "2026-05-08T12:01:00Z",
        },
      });

      assert.ok(event, "DB-backed local comment chain must resolve without HTTP");
      assert.equal(event.event_type, "comment.created");
      assert.equal(event.object.parent_ap_id, `${BRIDGE_ORIGIN}/users/alice/comment/200`);
      assert.equal(event.object.post_ap_id, `${BRIDGE_ORIGIN}/users/alice/post/100`);
      assert.equal(event.object.post_lemmy_id, 0);
    },
  );
}


async function testStoredActivitiesLoadByActivityIdAndRenderCreate(): Promise<void> {
  await withPublishedObjectDatabase(
    [
      {
        actor_username: "alice",
        actor_url: `${BRIDGE_ORIGIN}/users/alice`,
        community_actor_url: COMMUNITY_URL,
        activity_id: `${BRIDGE_ORIGIN}/users/alice/activities/create/post/100`,
        object_id: `${BRIDGE_ORIGIN}/users/alice/post/100`,
        kind: "post",
        title: "Stored post",
        body_markdown: "hello from discord",
        in_reply_to_object_id: null,
      },
      {
        actor_username: "alice",
        actor_url: `${BRIDGE_ORIGIN}/users/alice`,
        community_actor_url: COMMUNITY_URL,
        activity_id: `${BRIDGE_ORIGIN}/users/alice/activities/create/comment/200`,
        object_id: `${BRIDGE_ORIGIN}/users/alice/comment/200`,
        kind: "comment",
        title: null,
        body_markdown: "first reply",
        in_reply_to_object_id: `${BRIDGE_ORIGIN}/users/alice/post/100`,
      },
    ],
    async (databaseUrl) => {
      const storedPost = await loadPublishedActivityObjectByActivityIdForDatabaseUrl(
        databaseUrl,
        `${BRIDGE_ORIGIN}/users/alice/activities/create/post/100`,
      );
      assert.ok(storedPost, "stored post Create must be readable by activity id");

      const postCreate = buildPublishedCreateActivityJson(storedPost);
      const postObject = postCreate.object as Record<string, unknown>;
      assert.equal(postCreate.type, "Create");
      assert.equal(postCreate.id, `${BRIDGE_ORIGIN}/users/alice/activities/create/post/100`);
      assert.equal(postCreate.actor, CANONICAL_ALICE_ACTOR);
      assert.deepEqual(postCreate.to, [PUBLIC_IRI, COMMUNITY_URL]);
      assert.deepEqual(postCreate.cc, [CANONICAL_ALICE_ACTOR]);
      assert.equal(postObject.type, "Page");
      assert.equal(postObject.id, `${BRIDGE_ORIGIN}/users/alice/post/100`);
      assert.equal(postObject.content, "<p>hello from discord</p>");
      assert.equal(postObject.published, "2026-05-08T12:00:00Z");

      const storedComment = await loadPublishedActivityObjectByActivityIdForDatabaseUrl(
        databaseUrl,
        `${BRIDGE_ORIGIN}/users/alice/activities/create/comment/200`,
      );
      assert.ok(storedComment, "stored comment Create must be readable by activity id");

      const commentCreate = buildPublishedCreateActivityJson(storedComment);
      const commentObject = commentCreate.object as Record<string, unknown>;
      assert.equal(commentCreate.type, "Create");
      assert.equal(commentCreate.actor, CANONICAL_ALICE_ACTOR);
      assert.deepEqual(commentCreate.to, [PUBLIC_IRI, COMMUNITY_URL]);
      assert.equal(commentObject.type, "Note");
      assert.equal(commentObject.id, `${BRIDGE_ORIGIN}/users/alice/comment/200`);
      assert.equal(commentObject.inReplyTo, `${BRIDGE_ORIGIN}/users/alice/post/100`);
    },
  );
}

async function testStoredCreateActivityRoutesAreFetchable(): Promise<void> {
  await withPublishedObjectDatabase(
    [
      {
        actor_username: "alice",
        actor_url: `${BRIDGE_ORIGIN}/users/alice`,
        community_actor_url: COMMUNITY_URL,
        activity_id: `${BRIDGE_ORIGIN}/users/alice/activities/create/post/100`,
        object_id: `${BRIDGE_ORIGIN}/users/alice/post/100`,
        kind: "post",
        title: "Stored post",
        body_markdown: "hello from discord",
        in_reply_to_object_id: null,
      },
      {
        actor_username: "alice",
        actor_url: `${BRIDGE_ORIGIN}/users/alice`,
        community_actor_url: COMMUNITY_URL,
        activity_id: `${BRIDGE_ORIGIN}/users/alice/activities/create/comment/200`,
        object_id: `${BRIDGE_ORIGIN}/users/alice/comment/200`,
        kind: "comment",
        title: null,
        body_markdown: "first reply",
        in_reply_to_object_id: `${BRIDGE_ORIGIN}/users/alice/post/100`,
      },
    ],
    async (databaseUrl) => {
      const app = createGatewayApp(buildRouteTestConfig(databaseUrl));

      const postResponse = await app.request("/users/alice/activities/create/post/100", {
        headers: { Accept: "application/activity+json" },
      });
      assert.equal(postResponse.status, 200);
      assert.match(postResponse.headers.get("content-type") ?? "", /application\/activity\+json/);
      const postCreate = await postResponse.json() as Record<string, unknown>;
      assert.equal(postCreate.type, "Create");
      assert.equal(postCreate.id, `${BRIDGE_ORIGIN}/users/alice/activities/create/post/100`);
      assert.equal((postCreate.object as Record<string, unknown>).type, "Page");

      const commentResponse = await app.request("/users/alice/activities/create/comment/200", {
        headers: { Accept: "application/activity+json" },
      });
      assert.equal(commentResponse.status, 200);
      const commentCreate = await commentResponse.json() as Record<string, unknown>;
      assert.equal(commentCreate.type, "Create");
      assert.equal((commentCreate.object as Record<string, unknown>).type, "Note");

      const wrongKindResponse = await app.request("/users/alice/activities/create/comment/100");
      assert.equal(wrongKindResponse.status, 404);

      const wrongActorResponse = await app.request("/users/bob/activities/create/post/100");
      assert.equal(wrongActorResponse.status, 404);

      const missingResponse = await app.request("/users/alice/activities/create/post/missing");
      assert.equal(missingResponse.status, 404);
    },
  );
}

async function testActivityLookupHandlesUnknownAndMissingTable(): Promise<void> {
  await withPublishedObjectDatabase(
    [
      {
        actor_username: "alice",
        actor_url: `${BRIDGE_ORIGIN}/users/alice`,
        community_actor_url: COMMUNITY_URL,
        activity_id: `${BRIDGE_ORIGIN}/users/alice/activities/create/post/100`,
        object_id: `${BRIDGE_ORIGIN}/users/alice/post/100`,
        kind: "post",
        title: "Stored post",
        body_markdown: "hello from discord",
        in_reply_to_object_id: null,
      },
    ],
    async (databaseUrl) => {
      const missing = await loadPublishedActivityObjectByActivityIdForDatabaseUrl(
        databaseUrl,
        `${BRIDGE_ORIGIN}/users/alice/activities/create/post/missing`,
      );
      assert.equal(missing, null);
    },
  );

  await withEmptyDatabase(async (databaseUrl) => {
    const missingTable = await loadPublishedActivityObjectByActivityIdForDatabaseUrl(
      databaseUrl,
      `${BRIDGE_ORIGIN}/users/alice/activities/create/post/100`,
    );
    assert.equal(missingTable, null);
  });
}

async function testStoredObjectsRemainReadableAcrossFreshDbOpens(): Promise<void> {
  await withPublishedObjectDatabase(
    [
      {
        actor_username: "alice",
        actor_url: `${BRIDGE_ORIGIN}/users/alice`,
        community_actor_url: COMMUNITY_URL,
        activity_id: `${BRIDGE_ORIGIN}/users/alice/activities/create/post/999`,
        object_id: `${BRIDGE_ORIGIN}/users/alice/post/999`,
        kind: "post",
        title: "Restart-safe post",
        body_markdown: "persist me",
        in_reply_to_object_id: null,
      },
    ],
    async (databaseUrl) => {
      const firstRead = await loadPublishedActivityObjectByObjectIdForDatabaseUrl(
        databaseUrl,
        `${BRIDGE_ORIGIN}/users/alice/post/999`,
      );
      const secondRead = await loadPublishedActivityObjectByObjectIdForDatabaseUrl(
        databaseUrl,
        `${BRIDGE_ORIGIN}/users/alice/post/999`,
      );

      assert.ok(firstRead, "first DB open must see the stored object");
      assert.ok(secondRead, "second DB open must see the same stored object");
      assert.equal(secondRead.objectId, firstRead.objectId);
    },
  );
}

async function withPublishedObjectDatabase(
  rows: Array<Record<string, string | null>>,
  fn: (databaseUrl: string) => Promise<void>,
): Promise<void> {
  const tempDir = mkdtempSync(path.join(tmpdir(), "published-object-store-"));
  const databasePath = path.join(tempDir, "published-objects.sqlite3");
  const databaseUrl = `sqlite:///${databasePath}`;

  try {
    execFileSync(
      "../.venv/bin/python",
      ["-c", buildDatabaseBootstrapScript()],
      {
        cwd: process.cwd(),
        env: {
          ...process.env,
          BRIDGE_TEST_DB_PATH: databasePath,
          BRIDGE_TEST_ROWS_JSON: JSON.stringify(rows),
        },
      },
    );
    await fn(databaseUrl);
  } finally {
    rmSync(tempDir, { force: true, recursive: true });
  }
}


async function withEmptyDatabase(fn: (databaseUrl: string) => Promise<void>): Promise<void> {
  const tempDir = mkdtempSync(path.join(tmpdir(), "published-object-store-empty-"));
  const databasePath = path.join(tempDir, "empty.sqlite3");
  const databaseUrl = `sqlite:///${databasePath}`;

  try {
    execFileSync(
      "../.venv/bin/python",
      ["-c", "import os, sqlite3; sqlite3.connect(os.environ['BRIDGE_TEST_DB_PATH']).close()"],
      {
        cwd: process.cwd(),
        env: {
          ...process.env,
          BRIDGE_TEST_DB_PATH: databasePath,
        },
      },
    );
    await fn(databaseUrl);
  } finally {
    rmSync(tempDir, { force: true, recursive: true });
  }
}

function buildRouteTestConfig(databaseUrl: string): GatewayConfig {
  // Route-level tests only exercise DB-backed dereference routes, but the full
  // gateway app still needs a complete config object for middleware setup.
  return {
    actorIdentifier: "bridge",
    actorName: "Bridge",
    actorSummary: "Bridge summary",
    bridgePrivateKeyJwkJson: null,
    bridgePublicKeyJwkJson: null,
    communityActorId: null,
    databaseUrl,
    fedifyOrigin: BRIDGE_ORIGIN,
    port: 3000,
    pythonBridgeEventsUrl: "http://127.0.0.1:8080/internal/activitypub/events",
    pythonBridgeSharedSecret: "secret",
    logLevel: "info",
  };
}

function buildDatabaseBootstrapScript(): string {
  return `
import json
import os
import sqlite3

db_path = os.environ["BRIDGE_TEST_DB_PATH"]
rows = json.loads(os.environ["BRIDGE_TEST_ROWS_JSON"])
connection = sqlite3.connect(db_path)
cursor = connection.cursor()
cursor.execute("""
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
""")
for row in rows:
    cursor.execute(
        """
        INSERT INTO published_activity_objects (
          actor_username,
          actor_url,
          community_actor_url,
          activity_id,
          object_id,
          kind,
          title,
          body_markdown,
          in_reply_to_object_id,
          discord_channel_id,
          discord_message_id,
          published_at,
          created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
        """,
        (
          row["actor_username"],
          row["actor_url"],
          row["community_actor_url"],
          row["activity_id"],
          row["object_id"],
          row["kind"],
          row["title"],
          row["body_markdown"],
          row["in_reply_to_object_id"],
          "2026-05-08T12:00:00Z",
          "2026-05-08T12:00:00Z",
        ),
    )
connection.commit()
connection.close()
`;
}

await main();
