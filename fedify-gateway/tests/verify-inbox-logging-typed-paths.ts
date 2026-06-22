/**
 * Verifies Stage 3 raw ActivityPub inbox capture readiness.
 *
 * Production Fedify still listens on the pre-cutover inbox routes, but the raw
 * capture boundary must already recognize future typed actor-specific inboxes
 * so Undo(Follow) payload recovery is not lost after Stage 4 cutover.
 */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";

import {
  buildActivityPubRequestData,
  classifyInboxPath,
} from "../src/routes/inbox-logging.js";

/** Exercise pure path classification and raw JSON capture on accepted paths. */
async function main(): Promise<void> {
  assert.deepEqual(classifyInboxPath("POST", "/inbox"), {
    kind: "shared",
    path: "/inbox",
  });
  assert.deepEqual(classifyInboxPath("POST", "/actors/users/alice/inbox"), {
    kind: "actor",
    actor: { kind: "user", identifier: "alice", tail: "users/alice" },
    path: "/actors/users/alice/inbox",
  });
  assert.deepEqual(classifyInboxPath("POST", "/actors/communities/python/inbox"), {
    kind: "actor",
    actor: { kind: "community", identifier: "python", tail: "communities/python" },
    path: "/actors/communities/python/inbox",
  });
  assert.deepEqual(classifyInboxPath("POST", "/actors/services/bridge/inbox"), {
    kind: "actor",
    actor: { kind: "service", identifier: "bridge", tail: "services/bridge" },
    path: "/actors/services/bridge/inbox",
  });

  assert.equal(classifyInboxPath("POST", "/actors/users/alice/inbox?debug=1")?.path, "/actors/users/alice/inbox");

  for (const [method, path] of [
    ["GET", "/inbox"],
    ["POST", "/actors/alice/inbox"],
    ["POST", "/actors/users/alice"],
    ["POST", "/actors/users/alice/outbox"],
    ["POST", "/actors/users/alice/inbox/extra"],
    ["POST", "/communities/python/inbox"],
    ["POST", "/users/alice/inbox"],
    ["POST", "/actors/user/alice/inbox"],
    ["POST", "/actors/users/alice%2Fbob/inbox"],
  ] as Array<[string, string]>) {
    assert.equal(classifyInboxPath(method, path), null, `${method} ${path} must not classify`);
  }

  const payload = {
    "@context": "https://www.w3.org/ns/activitystreams",
    id: "https://remote.example/activities/undo/1",
    type: "Undo",
    object: { type: "Follow", id: "https://remote.example/activities/follow/1" },
  };
  const rawBody = JSON.stringify(payload);
  const captured = await buildActivityPubRequestData({
    req: {
      raw: new Request("https://bridge.example/actors/users/alice/inbox", {
        method: "POST",
        body: rawBody,
      }),
    },
  }, "https://bridge.example");
  assert.deepEqual(captured.activitypubRawJson, payload);
  assert.equal(
    captured.activitypubRawBodySha256,
    createHash("sha256").update(rawBody).digest("hex"),
  );
  assert.deepEqual(captured.activitypubInboxOwner, {
    kind: "user",
    identifier: "alice",
    actorUrl: "https://bridge.example/actors/users/alice",
  });

  const ignored = await buildActivityPubRequestData({
    req: {
      raw: new Request("https://bridge.example/actors/users/alice/outbox", {
        method: "POST",
        body: rawBody,
      }),
    },
  }, "https://bridge.example");
  assert.deepEqual(ignored, {});

  const malformed = await buildActivityPubRequestData({
    req: {
      raw: new Request("https://bridge.example/actors/users/alice/inbox", {
        method: "POST",
        body: "not-json",
      }),
    },
  }, "https://bridge.example");
  assert.deepEqual(malformed, {});

  console.log("verify:inbox-logging-typed-paths passed");
}

await main();
