import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createServer } from "node:http";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { Create, Note, Page, Person, Source } from "@fedify/vocab";

import {
  normalizeCreateActivity,
  normalizeCreateActivityFromJson,
} from "../src/normalize.js";
import { deliverEventToPythonBridge } from "../src/python-bridge.js";

const TEST_ORIGIN = "https://forum.example/";
const TEST_ACTOR_URL = `${TEST_ORIGIN}u/alice`;
const TEST_COMMUNITY_URL = `${TEST_ORIGIN}c/general`;
const TEST_POST_ACTIVITY_URL = `${TEST_ORIGIN}activities/create/post-1`;
const TEST_COMMENT_ACTIVITY_URL = `${TEST_ORIGIN}activities/create/comment-1`;
const TEST_POST_URL = `${TEST_ORIGIN}post/123`;
const TEST_COMMENT_URL = `${TEST_ORIGIN}comment/456`;
const TEST_SHARED_SECRET = "secret";

// This script locks the contract between the Node gateway and the Python
// bridge by verifying both normalization paths and HTTP delivery shape.
const testsDir = dirname(fileURLToPath(import.meta.url));
const gatewayDir = resolve(testsDir, "..");
const repositoryRoot = resolve(gatewayDir, "..");
const pythonExecutable = resolve(repositoryRoot, ".venv", "bin", "python");

async function main(): Promise<void> {
  const actor = new Person({
    id: new URL(TEST_ACTOR_URL),
    preferredUsername: "alice",
    name: "Alice",
  });

  const postEvent = await normalizeCreateActivity(
    new Create({
      id: new URL(TEST_POST_ACTIVITY_URL),
      actor,
      object: new Page({
        id: new URL(TEST_POST_URL),
        name: "Bridge test post",
        audience: new URL(TEST_COMMUNITY_URL),
        source: new Source({
          content: "hello from page",
          mediaType: "text/markdown",
        }),
        url: new URL(TEST_POST_URL),
      }),
    }),
  );

  const commentEvent = await normalizeCreateActivity(
    new Create({
      id: new URL(TEST_COMMENT_ACTIVITY_URL),
      actor,
      object: new Note({
        id: new URL(TEST_COMMENT_URL),
        audience: new URL(TEST_COMMUNITY_URL),
        source: new Source({
          content: "hello from note",
          mediaType: "text/markdown",
        }),
        replyTarget: new URL(TEST_POST_URL),
        url: new URL(TEST_COMMENT_URL),
      }),
    }),
  );

  assert.ok(postEvent);
  assert.ok(commentEvent);

  const announceWrappedComment = await normalizeCreateActivityFromJson({
    id: `${TEST_ORIGIN}activities/announce/comment-1`,
    type: "Create",
    actor: TEST_ACTOR_URL,
    object: {
      id: TEST_COMMENT_URL,
      type: "Note",
      audience: TEST_COMMUNITY_URL,
      source: {
        content: "hello from note",
        mediaType: "text/markdown",
      },
      inReplyTo: TEST_POST_URL,
      url: TEST_COMMENT_URL,
    },
  });

  assert.ok(announceWrappedComment);
  assert.equal(announceWrappedComment.event_type, "comment.created");
  assert.equal(announceWrappedComment.object.post_ap_id, TEST_POST_URL);

  await verifyHttpDelivery(commentEvent);
  validateWithPythonSchema(postEvent);
  validateWithPythonSchema(commentEvent);
  validateWithPythonSchema(announceWrappedComment);
  console.log("Fedify -> Python contract verification passed");
}

async function verifyHttpDelivery(
  event: NonNullable<Awaited<ReturnType<typeof normalizeCreateActivity>>>,
) {
  // Use a local ephemeral server so the gateway-side HTTP helper can be tested
  // without a running Python process.
  let receivedBody = "";
  let receivedAuth = "";
  let receivedDeliveryId = "";

  const server = createServer((request, response) => {
    receivedAuth = request.headers.authorization ?? "";
    receivedDeliveryId = String(request.headers["x-bridge-delivery-id"] ?? "");
    request.setEncoding("utf8");
    request.on("data", (chunk) => {
      receivedBody += chunk;
    });
    request.on("end", () => {
      response.statusCode = 200;
      response.end("ok");
    });
  });

  await new Promise<void>((resolve) =>
    server.listen(0, "127.0.0.1", () => resolve()),
  );
  const address = server.address();
  if (address == null || typeof address === "string") {
    throw new Error("Could not determine verification server address");
  }

  try {
    await deliverEventToPythonBridge(
      `http://127.0.0.1:${address.port}/internal/activitypub/events`,
      TEST_SHARED_SECRET,
      event,
    );
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }

  assert.equal(receivedAuth, `Bearer ${TEST_SHARED_SECRET}`);
  assert.equal(receivedDeliveryId, event.delivery_id);
  assert.deepEqual(JSON.parse(receivedBody), event);
}

function validateWithPythonSchema(
  event: NonNullable<Awaited<ReturnType<typeof normalizeCreateActivity>>>,
): void {
  // Final validation delegates to the real Python Pydantic model so both
  // runtimes agree on the accepted event schema.
  const result = spawnSync(
    pythonExecutable,
    [
      "-c",
      [
        "import json, sys",
        "from src.activitypub_models import ActivityPubEvent",
        "ActivityPubEvent.model_validate(json.load(sys.stdin))",
      ].join("\n"),
    ],
    {
      cwd: repositoryRoot,
      encoding: "utf8",
      input: JSON.stringify(event),
    },
  );

  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(
      `Python schema validation failed: ${result.stderr || result.stdout}`,
    );
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
