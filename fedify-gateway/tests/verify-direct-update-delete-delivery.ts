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
  await testDirectPersonUpdatePreservesCanonicalPublicAudience();
  await testUnknownDirectPersonDeleteReservesProvisionalCommentTombstone();
  await testUnknownPostShapedNoteDeleteCannotBeMistakenForGroupPost();
  await testKnownPersonRootDeleteUsesCanonicalTargetNotUrlPath();
  await testUnknownGroupAddressedDeleteDoesNotCreatePersonTombstone();
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
        audience: new URL(`${TEST_ORIGIN}actors/communities/hackers`),
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
      ccs: [new URL(`${TEST_ORIGIN}actors/communities/hackers`)],
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

/** Signed Update for an existing Person root preserves normalized Public addressing. */
async function testDirectPersonUpdatePreservesCanonicalPublicAudience(): Promise<void> {
  const capture = await startCaptureServer();
  try {
    const actor = new URL('https://remote.example/users/bob');
    const objectId = new URL('https://remote.example/notes/root');
    const publicUri = new URL('https://www.w3.org/ns/activitystreams#Public');
    const activity = new Update({
      id: new URL('https://remote.example/activities/edit-root'), actor,
      tos: [publicUri],
      object: new Note({
        id: objectId, attribution: actor, tos: [publicUri],
        source: new Source({ content: 'Правка 🦊', mediaType: 'text/markdown' }),
      }),
    });
    // Only identity/audience normalization is under test; profile loading is
    // an optional presentation edge, independent of admission.
    Object.assign(activity, { getActor: async () => null });
    await deliverDirectUpdateActivity(buildConfig(capture.url), activity);
    assert.ok(capture.body != null);
    const payload = JSON.parse(capture.body) as Record<string, unknown>;
    assert.equal(payload.event_type, 'remote.person.updated');
    const source = payload.source_activity_json as Record<string, unknown>;
    const note = source.object as Record<string, unknown>;
    assert.ok([source.to].flat().includes(publicUri.href));
    assert.ok([note.to].flat().includes(publicUri.href));
    assert.equal(payload.verified_sender_actor_id, actor.href);
  } finally {
    await capture.close();
  }
}

/** Signed Delete of an unseen Note cannot guess root vs reply from its URL. */
async function testUnknownDirectPersonDeleteReservesProvisionalCommentTombstone(): Promise<void> {
  const capture = await startCaptureServer();
  try {
    const activity = new Delete({
      id: new URL('https://remote.example/activities/delete-unseen'),
      actor: new URL('https://remote.example/users/bob'),
      object: new URL('https://remote.example/notes/unseen'),
    });
    await deliverDirectDeleteActivity(buildConfig(capture.url), activity);
    assert.ok(capture.body != null);
    const payload = JSON.parse(capture.body) as Record<string, unknown>;
    assert.equal(payload.event_type, 'remote.person.comment.deleted');
    assert.equal((payload.object as Record<string, unknown>).parent_ap_id, null);
    assert.equal((payload.object as Record<string, unknown>).ap_id,
                 'https://remote.example/notes/unseen');
  } finally {
    await capture.close();
  }
}

/** NodeBB Person replies may use /post/ IDs despite being Note comments. */
async function testUnknownPostShapedNoteDeleteCannotBeMistakenForGroupPost(): Promise<void> {
  const capture = await startCaptureServer();
  try {
    const activity = new Delete({
      id: new URL('https://nodebb.example/activities/delete-68'),
      actor: new URL('https://nodebb.example/uid/2'),
      object: new URL('https://nodebb.example/post/68'),
    });
    await deliverDirectDeleteActivity(buildConfig(capture.url), activity);
    assert.ok(capture.body != null);
    const payload = JSON.parse(capture.body) as Record<string, unknown>;
    assert.equal(payload.event_type, 'remote.person.comment.deleted');
    assert.equal((payload.object as Record<string, unknown>).ap_id,
                 'https://nodebb.example/post/68');
  } finally {
    await capture.close();
  }
}

/** A known NodeBB /post/ ID is still a Person root when storage proves it. */
async function testKnownPersonRootDeleteUsesCanonicalTargetNotUrlPath(): Promise<void> {
  const root = 'https://nodebb.example/post/64';
  const capture = await startCaptureServer({
    publication_context: 'remote_person_feed', root_object_id: root,
    actor_id: 'https://nodebb.example/uid/2',
  });
  try {
    const activity = new Delete({
      id: new URL('https://nodebb.example/activities/delete-64'),
      actor: new URL('https://nodebb.example/uid/2'), object: new URL(root),
    });
    await deliverDirectDeleteActivity(buildConfig(capture.url), activity);
    assert.ok(capture.body != null);
    const event = JSON.parse(capture.body) as Record<string, unknown>;
    assert.equal(event.event_type, 'remote.person.deleted');
    assert.equal((event.object as Record<string, unknown>).ap_id, root);
  } finally {
    await capture.close();
  }
}

/** Unknown direct Deletes addressed to a local Group cannot enter Person storage. */
async function testUnknownGroupAddressedDeleteDoesNotCreatePersonTombstone(): Promise<void> {
  const capture = await startCaptureServer();
  try {
    const group = new URL(`${TEST_ORIGIN}actors/communities/hackers`);
    const activity = new Delete({
      id: new URL('https://lemmy.example/activities/delete-79'),
      actor: new URL('https://lemmy.example/u/admin'),
      object: new URL('https://lemmy.example/post/79'),
      tos: [group],
    });
    await deliverDirectDeleteActivity(buildConfig(capture.url), activity);
    // The context-free test route cannot prove the server-owned Group: its
    // ordinary group ingress rejects it rather than staging a Person event.
    assert.equal(capture.body, null);
  } finally {
    await capture.close();
  }
}

async function startCaptureServer(knownTarget?: Record<string, unknown>): Promise<{
  url: string;
  body: string | null;
  close: () => Promise<void>;
}> {
  /** Start one fake Python bridge boundary that records the last request body. */
  let body: string | null = null;
  const server = createServer((req, res) => {
    if ((req.url ?? "").startsWith("/internal/fedify/")) {
      if (knownTarget != null && (req.url ?? "").includes("conversation-targets/resolve")) {
        res.statusCode = 200;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify(knownTarget));
        return;
      }
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
