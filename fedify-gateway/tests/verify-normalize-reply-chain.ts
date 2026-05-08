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
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";

import { normalizeCreateActivityFromJson } from "../src/normalize.js";

const LEMMY_ORIGIN = "https://lemmy.example";
const COMMUNITY_URL = `${LEMMY_ORIGIN}/c/testcommunity`;

async function main(): Promise<void> {
  await testLemmyReplyToGatewayPost();
  await testLemmyReplyToGatewayComment();
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

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
