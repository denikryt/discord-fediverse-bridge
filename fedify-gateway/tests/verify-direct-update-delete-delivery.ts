/**
 * Regression tests for direct inbound Update/Delete delivery.
 *
 * Some remote servers deliver direct Update/Delete without an Announce wrapper.
 * The gateway must normalize those activities and forward them to Python.
 */

import assert from "node:assert/strict";
import { createServer } from "node:http";

import { Delete, Note, Source, Update } from "@fedify/vocab";

import {
  deliverDirectDeleteActivity,
  deliverDirectUpdateActivity,
} from "../src/federation.js";
import type { GatewayContextData } from "../src/config.js";

const TEST_ORIGIN = "https://discord-bridge.example.com/";
const REMOTE_ORIGIN = "https://lemmy.example/";

async function main(): Promise<void> {
  await testDirectUpdateDeliversCommentUpdatedEvent();
  await testDirectDeleteDeliversCommentDeletedEvent();
  console.log("verify:direct-update-delete-delivery passed");
}

/**
 * Action: the gateway receives a direct Update(Note) activity.
 * Expected: Python receives `comment.updated` with `source_activity_json`.
 */
async function testDirectUpdateDeliversCommentUpdatedEvent(): Promise<void> {
  const capture = await startCaptureServer();
  try {
    const activity = new Update({
      id: new URL(`${REMOTE_ORIGIN}activities/update/comment-1`),
      actor: new URL(`${REMOTE_ORIGIN}u/admin`),
      object: new Note({
        id: new URL(`${REMOTE_ORIGIN}comment/125`),
        attribution: new URL(`${REMOTE_ORIGIN}u/admin`),
        audience: new URL(`${TEST_ORIGIN}communities/hackers`),
        source: new Source({
          content: "Updated comment text",
          mediaType: "text/markdown",
        }),
        replyTarget: new URL(`${REMOTE_ORIGIN}post/35`),
      }),
    });

    await deliverDirectUpdateActivity(buildConfig(capture.url), activity);

    assert.ok(capture.body != null);
    const payload = JSON.parse(capture.body) as Record<string, unknown>;
    assert.equal(payload.event_type, "comment.updated");
    assert.equal((payload.object as Record<string, unknown>).ap_id, `${REMOTE_ORIGIN}comment/125`);
    assert.equal((payload as Record<string, unknown>).source_announce_id, null);
    assert.equal(((payload as Record<string, unknown>).source_activity_json as Record<string, unknown>).type, "Update");
  } finally {
    await capture.close();
  }
}

/**
 * Action: the gateway receives a direct Delete(comment-url) activity.
 * Expected: Python receives `comment.deleted` with `source_activity_json`.
 */
async function testDirectDeleteDeliversCommentDeletedEvent(): Promise<void> {
  const capture = await startCaptureServer();
  try {
    const activity = new Delete({
      id: new URL(`${REMOTE_ORIGIN}activities/delete/comment-1`),
      actor: new URL(`${REMOTE_ORIGIN}u/admin`),
      object: new URL(`${REMOTE_ORIGIN}comment/125`),
      tos: [new URL("https://www.w3.org/ns/activitystreams#Public")],
      ccs: [new URL(`${TEST_ORIGIN}communities/hackers`)],
    });

    await deliverDirectDeleteActivity(buildConfig(capture.url), activity);

    assert.ok(capture.body != null);
    const payload = JSON.parse(capture.body) as Record<string, unknown>;
    assert.equal(payload.event_type, "comment.deleted");
    assert.equal((payload.object as Record<string, unknown>).ap_id, `${REMOTE_ORIGIN}comment/125`);
    assert.equal((payload as Record<string, unknown>).source_announce_id, null);
    assert.equal(((payload as Record<string, unknown>).source_activity_json as Record<string, unknown>).type, "Delete");
  } finally {
    await capture.close();
  }
}

async function startCaptureServer(): Promise<{
  url: string;
  body: string | null;
  close: () => Promise<void>;
}> {
  /** Start one fake Python bridge boundary that records the last request body. */
  let body: string | null = null;
  const server = createServer((req, res) => {
    if ((req.url ?? "").startsWith("/internal/fedify/")) {
      res.statusCode = 404;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ detail: "not found" }));
      return;
    }
    req.setEncoding("utf8");
    req.on("data", (chunk) => {
      body = (body ?? "") + chunk;
    });
    req.on("end", () => {
      res.statusCode = 200;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ status: "processed" }));
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
  const address = server.address();
  if (address == null || typeof address === "string") {
    throw new Error("Could not determine capture server address");
  }
  return {
    url: `http://127.0.0.1:${address.port}/internal/activitypub/events`,
    get body() {
      return body;
    },
    close: async () => {
      await new Promise<void>((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
    },
  };
}

function buildConfig(eventsUrl: string): GatewayContextData {
  /** Build the minimal gateway context required by direct delivery helpers. */
  return {
    actorIdentifier: "bridge",
    actorName: "Bridge",
    actorSummary: "Bridge summary",
    fedifyOrigin: TEST_ORIGIN,
    port: 3000,
    pythonBridgeInternalUrl: eventsUrl.replace("/internal/activitypub/events", ""),
    pythonBridgeSharedSecret: "secret",
    logLevel: "info",
    activitypubRawBodySha256: undefined,
    activitypubRawJson: undefined,
  };
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
