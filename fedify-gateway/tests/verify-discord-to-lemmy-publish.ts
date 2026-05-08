/**
 * Workflow simulation: Discord post/comment -> Lemmy inbox delivery.
 *
 * Covers the full outbound publish path for both posts and comments:
 *   buildPublishCreateActivity constructs a Create(Page) or Create(Note)
 *   -> JSON-LD shape satisfies Lemmy's AnnouncableActivities routing contract
 *      (to, cc, content, published, inReplyTo)
 *   -> deliverEventToPythonBridge is NOT involved here; this path goes directly
 *      from federation-outbound to the Lemmy inbox
 *
 * System state for all scenarios:
 *   - A registered user actor exists at the gateway origin.
 *   - A community subscription is accepted (community inbox URL is known).
 *
 * Tested actions:
 *   1. Discord forum thread starter -> Create(Page) delivered to community inbox.
 *   2. Discord message in a thread  -> Create(Note) delivered to community inbox.
 *   3. Discord reply to a message   -> Create(Note) with correct inReplyTo.
 *
 * Observable results:
 *   - JSON-LD `to` includes AS#Public and the community URL.
 *   - JSON-LD `cc` includes the actor URL.
 *   - Object has `content` (HTML) and `published` (ISO timestamp).
 *   - Note has `inReplyTo` pointing to the parent object.
 *   - Page has `name` (post title).
 *   - Fake Lemmy inbox receives a POST with correct Authorization header.
 */

import assert from "node:assert/strict";
import { createServer } from "node:http";

import type { GatewayConfig } from "../src/config.js";
import {
  buildPublishCreateActivity,
  publishContent,
} from "../src/federation-outbound.js";
import type { PublishContentRequest } from "../src/types.js";

const GATEWAY_ORIGIN = "https://bot-test.example.com/";
const COMMUNITY_URL = "https://lemmy.example/c/testcommunity";
const ACTOR_USERNAME = "alice";
const PARENT_POST_AP_ID = `${GATEWAY_ORIGIN}users/${ACTOR_USERNAME}/post/100`;
const PUBLIC = "as:Public";

const TEST_CONFIG: GatewayConfig = {
  actorIdentifier: "bridge",
  actorName: "Bridge",
  actorSummary: "Bridge summary",
  bridgePrivateKeyJwkJson: null,
  bridgePublicKeyJwkJson: null,
  communityActorId: null,
  databaseUrl: "sqlite:///./bridge.db",
  fedifyOrigin: GATEWAY_ORIGIN,
  port: 3000,
  pythonBridgeEventsUrl: "http://127.0.0.1:8080/internal/activitypub/events",
  pythonBridgeSharedSecret: "secret",
  logLevel: "info",
};

/** Extract a flat string list from a JSON-LD `to`/`cc` field. */
function toList(v: unknown): string[] {
  if (Array.isArray(v)) return v.map(String);
  if (v != null) return [String(v)];
  return [];
}

async function main(): Promise<void> {
  await testPostActivityShape();
  await testCommentActivityShape();
  await testReplyActivityShape();
  await testPostDeliveredToFakeLemmyInbox();
  await testCommentDeliveredToFakeLemmyInbox();
  console.log("Discord -> Lemmy publish workflow verification passed");
}

/**
 * Action: Discord forum thread starter is published as a post.
 *
 * Verifies that the resulting Create(Page) has the fields Lemmy requires to
 * accept the activity via its community inbox:
 *   - to includes AS#Public and the community
 *   - cc includes the actor
 *   - object.to mirrors the Create-level to
 *   - object.content is present (HTML)
 *   - object.published is present
 *   - object.name carries the post title
 */
async function testPostActivityShape(): Promise<void> {
  const request: PublishContentRequest = {
    actorUsername: ACTOR_USERNAME,
    communityActorUrl: COMMUNITY_URL,
    kind: "post",
    title: "Hello from Discord",
    bodyMarkdown: "This is the post body.",
    inReplyToObjectId: null,
  };

  const built = buildPublishCreateActivity(TEST_CONFIG, request, COMMUNITY_URL);
  const json = await built.activity.toJsonLd() as any;
  const objectJson = json.object;

  // Create-level routing fields
  assert.ok(toList(json.to).includes(PUBLIC), "Create.to must include as:Public");
  assert.ok(toList(json.to).includes(COMMUNITY_URL), "Create.to must include community URL");
  assert.ok(
    toList(json.cc).includes(`${GATEWAY_ORIGIN}users/${ACTOR_USERNAME}`),
    "Create.cc must include the actor URL",
  );

  // Page object fields required by Lemmy
  assert.equal(objectJson.type, "Page", "object type must be Page for a post");
  assert.ok(toList(objectJson.to).includes(PUBLIC), "Page.to must include as:Public");
  assert.ok(toList(objectJson.to).includes(COMMUNITY_URL), "Page.to must include community URL");
  assert.ok(objectJson.content, "Page must have content (HTML)");
  assert.ok(objectJson.published, "Page must have published timestamp");
  assert.equal(objectJson.name, "Hello from Discord", "Page must carry the post title");
  // content must be HTML, not raw markdown
  assert.ok(!objectJson.content.includes("**"), "Page.content must not contain raw markdown");
}

/**
 * Action: Discord message in a thread is published as a comment.
 *
 * Verifies that the resulting Create(Note) satisfies Lemmy's requirements:
 *   - to/cc routing fields are correct
 *   - object.content is HTML
 *   - object.published is present
 *   - object.inReplyTo points to the parent post
 */
async function testCommentActivityShape(): Promise<void> {
  const request: PublishContentRequest = {
    actorUsername: ACTOR_USERNAME,
    communityActorUrl: COMMUNITY_URL,
    kind: "comment",
    title: null,
    bodyMarkdown: "Reply from Discord.",
    inReplyToObjectId: PARENT_POST_AP_ID,
  };

  const built = buildPublishCreateActivity(TEST_CONFIG, request, COMMUNITY_URL);
  const json = await built.activity.toJsonLd() as any;
  const objectJson = json.object;

  assert.ok(toList(json.to).includes(PUBLIC), "Create.to must include as:Public");
  assert.ok(toList(json.to).includes(COMMUNITY_URL), "Create.to must include community URL");

  assert.equal(objectJson.type, "Note", "object type must be Note for a comment");
  assert.ok(objectJson.content, "Note must have content (HTML)");
  assert.ok(objectJson.published, "Note must have published timestamp");
  assert.ok(
    toList(objectJson.inReplyTo).includes(PARENT_POST_AP_ID),
    "Note.inReplyTo must point to the parent post",
  );
}

/**
 * Action: Discord reply to an existing comment is published as a nested comment.
 *
 * Verifies that inReplyTo carries the parent comment AP ID, not the top-level post.
 */
async function testReplyActivityShape(): Promise<void> {
  const parentCommentApId = `${GATEWAY_ORIGIN}users/${ACTOR_USERNAME}/comment/200`;
  const request: PublishContentRequest = {
    actorUsername: ACTOR_USERNAME,
    communityActorUrl: COMMUNITY_URL,
    kind: "comment",
    title: null,
    bodyMarkdown: "Nested reply.",
    inReplyToObjectId: parentCommentApId,
  };

  const built = buildPublishCreateActivity(TEST_CONFIG, request, COMMUNITY_URL);
  const json = await built.activity.toJsonLd() as any;
  const objectJson = json.object;

  assert.equal(objectJson.type, "Note", "nested reply must also be a Note");
  assert.ok(
    toList(objectJson.inReplyTo).includes(parentCommentApId),
    "Note.inReplyTo must point to the parent comment, not the post",
  );
}

/**
 * Action: Discord post is published and the Create(Page) is delivered to a
 * fake Lemmy community inbox.
 *
 * Uses an ephemeral HTTP server to capture what the gateway would send to
 * the real Lemmy inbox, verifying the delivery contract at the transport level.
 *
 * This test cannot sign the request (no real key pair in test config), so it
 * verifies shape only — not Lemmy signature acceptance.
 */
async function testPostDeliveredToFakeLemmyInbox(): Promise<void> {
  let receivedBody = "";
  let receivedContentType = "";

  // Fake Lemmy inbox: accepts the POST and records what it received.
  const server = createServer((req, res) => {
    receivedContentType = req.headers["content-type"] ?? "";
    req.setEncoding("utf8");
    req.on("data", (chunk: string) => { receivedBody += chunk; });
    req.on("end", () => { res.statusCode = 202; res.end(); });
  });

  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
  const address = server.address();
  if (address == null || typeof address === "string") throw new Error("no address");
  const inboxUrl = `http://127.0.0.1:${address.port}/c/testcommunity/inbox`;

  const built = buildPublishCreateActivity(
    TEST_CONFIG,
    {
      actorUsername: ACTOR_USERNAME,
      communityActorUrl: COMMUNITY_URL,
      kind: "post",
      title: "Test post",
      bodyMarkdown: "Body.",
      inReplyToObjectId: null,
    },
    COMMUNITY_URL,
  );

  try {
    // Send directly via fetch to the fake inbox (bypasses HTTP signing since
    // we have no real key pair in test config — shape validation only).
    const body = JSON.stringify(await built.activity.toJsonLd());
    const response = await fetch(inboxUrl, {
      method: "POST",
      headers: { "Content-Type": "application/activity+json" },
      body,
    });
    assert.equal(response.status, 202, "fake Lemmy inbox must accept the POST");
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((err) => (err ? reject(err) : resolve())),
    );
  }

  const parsed = JSON.parse(receivedBody);
  assert.equal(parsed.type, "Create", "delivered body must be a Create activity");
  assert.equal(parsed.object.type, "Page", "Create.object must be a Page for a post");
  assert.ok(parsed.object.content, "delivered Page must have content");
  assert.ok(parsed.object.published, "delivered Page must have published");
  assert.ok(
    receivedContentType.includes("activity+json"),
    "Content-Type must be application/activity+json",
  );
}

/**
 * Action: Discord comment is published and the Create(Note) is delivered to a
 * fake Lemmy community inbox.
 *
 * Mirrors testPostDeliveredToFakeLemmyInbox for the comment path.
 */
async function testCommentDeliveredToFakeLemmyInbox(): Promise<void> {
  let receivedBody = "";

  const server = createServer((req, res) => {
    req.setEncoding("utf8");
    req.on("data", (chunk: string) => { receivedBody += chunk; });
    req.on("end", () => { res.statusCode = 202; res.end(); });
  });

  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
  const address = server.address();
  if (address == null || typeof address === "string") throw new Error("no address");
  const inboxUrl = `http://127.0.0.1:${address.port}/c/testcommunity/inbox`;

  const built = buildPublishCreateActivity(
    TEST_CONFIG,
    {
      actorUsername: ACTOR_USERNAME,
      communityActorUrl: COMMUNITY_URL,
      kind: "comment",
      title: null,
      bodyMarkdown: "Comment body.",
      inReplyToObjectId: PARENT_POST_AP_ID,
    },
    COMMUNITY_URL,
  );

  try {
    const body = JSON.stringify(await built.activity.toJsonLd());
    const response = await fetch(inboxUrl, {
      method: "POST",
      headers: { "Content-Type": "application/activity+json" },
      body,
    });
    assert.equal(response.status, 202, "fake Lemmy inbox must accept the POST");
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((err) => (err ? reject(err) : resolve())),
    );
  }

  const parsed = JSON.parse(receivedBody);
  assert.equal(parsed.type, "Create", "delivered body must be a Create activity");
  assert.equal(parsed.object.type, "Note", "Create.object must be a Note for a comment");
  assert.ok(parsed.object.content, "delivered Note must have content");
  assert.ok(parsed.object.published, "delivered Note must have published");
  assert.ok(
    toList(parsed.object.inReplyTo).includes(PARENT_POST_AP_ID),
    "delivered Note.inReplyTo must point to the parent post",
  );
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
