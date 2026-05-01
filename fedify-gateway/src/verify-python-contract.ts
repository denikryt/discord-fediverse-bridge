import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createServer } from "node:http";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { Create, Note, Page, Person, Source } from "@fedify/vocab";

import {
  normalizeCreateActivity,
  normalizeCreateActivityFromJson,
} from "./normalize.js";
import { deliverEventToPythonBridge } from "./python-bridge.js";

// This script locks the contract between the Node gateway and the Python
// bridge by verifying both normalization paths and HTTP delivery shape.
const gatewayDir = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(gatewayDir, "..", "..");
const pythonExecutable = resolve(repositoryRoot, ".venv", "bin", "python");

async function main(): Promise<void> {
  const actor = new Person({
    id: new URL("https://forum.example/u/alice"),
    preferredUsername: "alice",
    name: "Alice",
  });

  const postEvent = await normalizeCreateActivity(
    new Create({
      id: new URL("https://forum.example/activities/create/post-1"),
      actor,
      object: new Page({
        id: new URL("https://forum.example/post/123"),
        name: "Bridge test post",
        audience: new URL("https://forum.example/c/general"),
        source: new Source({
          content: "hello from page",
          mediaType: "text/markdown",
        }),
        url: new URL("https://forum.example/post/123"),
      }),
    }),
  );

  const commentEvent = await normalizeCreateActivity(
    new Create({
      id: new URL("https://forum.example/activities/create/comment-1"),
      actor,
      object: new Note({
        id: new URL("https://forum.example/comment/456"),
        audience: new URL("https://forum.example/c/general"),
        source: new Source({
          content: "hello from note",
          mediaType: "text/markdown",
        }),
        replyTarget: new URL("https://forum.example/post/123"),
        url: new URL("https://forum.example/comment/456"),
      }),
    }),
  );

  assert.ok(postEvent);
  assert.ok(commentEvent);

  const announceWrappedComment = normalizeCreateActivityFromJson({
    id: "https://forum.example/activities/announce/comment-1",
    type: "Create",
    actor: "https://forum.example/u/alice",
    object: {
      id: "https://forum.example/comment/456",
      type: "Note",
      audience: "https://forum.example/c/general",
      source: {
        content: "hello from note",
        mediaType: "text/markdown",
      },
      inReplyTo: "https://forum.example/post/123",
      url: "https://forum.example/comment/456",
    },
  });

  assert.ok(announceWrappedComment);
  assert.equal(announceWrappedComment.event_type, "comment.created");
  assert.equal(
    announceWrappedComment.object.post_ap_id,
    "https://forum.example/post/123",
  );

  await verifyHttpDelivery(commentEvent);
  validateWithPythonSchema(postEvent);
  validateWithPythonSchema(commentEvent);
  validateWithPythonSchema(announceWrappedComment);
  console.log("Fedify -> Python contract verification passed");
}

async function verifyHttpDelivery(event: NonNullable<Awaited<ReturnType<typeof normalizeCreateActivity>>>) {
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

  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
  const address = server.address();
  if (address == null || typeof address === "string") {
    throw new Error("Could not determine verification server address");
  }

  try {
    await deliverEventToPythonBridge(
      `http://127.0.0.1:${address.port}/internal/activitypub/events`,
      "secret",
      event,
    );
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }

  assert.equal(receivedAuth, "Bearer secret");
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
