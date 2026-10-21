/**
 * Regression test for the Accept(Follow) -> follow.accepted delivery path.
 *
 * A queued Accept uses Fedify's typed payload, even if a same-ID raw cache
 * entry is stale or forged. Only a reference to a known outbound Follow may
 * produce a lifecycle event for the Python bridge.
 */

import assert from "node:assert/strict";
import { createServer } from "node:http";

import { Accept } from "@fedify/vocab";

import { storeRawActivity } from "../src/activitypub-raw-cache.js";
import { buildFollowAcceptedEvent } from "../src/federation.js";
import { PythonBridgeClient } from "../src/python-bridge-client.js";
import type { GatewayConfig } from "../src/config.js";
import type { FollowAcceptedEvent } from "../src/types.js";

const TEST_ORIGIN = "https://discord-bridge.example.com/";
const LEMMY_ORIGIN = "https://lemmy.example/";
const TEST_SHARED_SECRET = "secret";

const FOLLOW_ACTIVITY_ID = `${TEST_ORIGIN}activities/follow/123/abc`;
const ACCEPT_ACTIVITY_ID = `${LEMMY_ORIGIN}activities/accept/xyz-001`;
const COMMUNITY_ACTOR_ID = `${LEMMY_ORIGIN}c/general`;
const STALE_ANNOUNCE_ID = `${LEMMY_ORIGIN}activities/announce/create/stale-001`;
const STALE_COMMENT_ACTIVITY_ID = `${TEST_ORIGIN}users/alice/activities/create/comment/stale-001`;

/**
 * The Accept payload as Lemmy sends it: object is the full Follow object,
 * not a bare string. This is the shape that exposed the cache-fallback bug.
 */
const ACCEPT_PAYLOAD = {
  "@context": "https://www.w3.org/ns/activitystreams",
  id: ACCEPT_ACTIVITY_ID,
  type: "Accept",
  actor: COMMUNITY_ACTOR_ID,
  object: {
    id: FOLLOW_ACTIVITY_ID,
    type: "Follow",
    actor: `${TEST_ORIGIN}actors/services/bridge`,
    object: COMMUNITY_ACTOR_ID,
  },
};

const STALE_ANNOUNCE_PAYLOAD = {
  "@context": "https://www.w3.org/ns/activitystreams",
  id: STALE_ANNOUNCE_ID,
  type: "Announce",
  actor: COMMUNITY_ACTOR_ID,
  object: {
    id: STALE_COMMENT_ACTIVITY_ID,
    type: "Create",
    actor: `${TEST_ORIGIN}users/alice`,
    object: {
      id: `${TEST_ORIGIN}users/alice/comment/200`,
      type: "Note",
      inReplyTo: `${TEST_ORIGIN}users/alice/post/100`,
    },
  },
};

async function main(): Promise<void> {
  await testCacheCannotProvideMissingFollowId();
  await testTypedAcceptWinsOverStaleRawPayload();
  await testDeliveryShapeMatchesPythonContract();
  console.log("Accept(Follow) -> follow.accepted path verification passed");
}

/**
 * A signed Accept without an object ID cannot borrow one from a later body
 * that reused its outer activity ID.
 */
async function testCacheCannotProvideMissingFollowId(): Promise<void> {
  // Hono can cache a body before Fedify rejects its signature; the typed
  // accepted activity remains the sole source for subscription state.
  storeRawActivity(ACCEPT_ACTIVITY_ID, ACCEPT_PAYLOAD);
  const activity = new Accept({
    id: new URL(ACCEPT_ACTIVITY_ID), actor: new URL(COMMUNITY_ACTOR_ID),
  });
  assert.equal(await buildFollowAcceptedEvent(activity, gatewayConfig()), null);
}

/**
 * Verify the current typed Accept wins over a stale raw cache entry.
 *
 * This matches the true correctness contract: delivery_id must come from the
 * current Accept.id and follow_activity_id must come from Accept.object.
 */
async function testTypedAcceptWinsOverStaleRawPayload(): Promise<void> {
  storeRawActivity(ACCEPT_ACTIVITY_ID, STALE_ANNOUNCE_PAYLOAD);
  const activity = new Accept({
    id: new URL(ACCEPT_ACTIVITY_ID),
    actor: new URL(COMMUNITY_ACTOR_ID),
    object: new URL(FOLLOW_ACTIVITY_ID),
  });

  const event = await buildFollowAcceptedEvent(activity, gatewayConfig());

  assert.ok(event, "Accept(Follow) must still produce an event");
  assert.equal(
    event.delivery_id,
    ACCEPT_ACTIVITY_ID,
    "delivery_id must come from the current Accept activity",
  );
  assert.equal(
    event.object.follow_activity_id,
    FOLLOW_ACTIVITY_ID,
    "follow_activity_id must come from the current Accept.object",
  );
  assert.equal(
    event.actor_id,
    COMMUNITY_ACTOR_ID,
    "actor_id must come from the current Accept.actor",
  );
}

/**
 * Verifies the full delivery shape: an event built from a typed Follow URL
 * reference is delivered to the Python bridge with the correct
 * fields, auth header, and delivery-id header.
 *
 * Uses an ephemeral HTTP server as a fake Python bridge boundary.
 */
async function testDeliveryShapeMatchesPythonContract(): Promise<void> {
  const event = await buildFollowAcceptedEvent(new Accept({
    id: new URL(ACCEPT_ACTIVITY_ID), actor: new URL(COMMUNITY_ACTOR_ID),
    object: new URL(FOLLOW_ACTIVITY_ID),
  }), gatewayConfig());
  assert.ok(event, "a typed Follow reference must produce a delivery event");

  let receivedBody = "";
  let receivedAuth = "";
  let receivedDeliveryId = "";

  const server = createServer((req, res) => {
    receivedAuth = req.headers.authorization ?? "";
    receivedDeliveryId = String(req.headers["x-bridge-delivery-id"] ?? "");
    req.setEncoding("utf8");
    req.on("data", (chunk) => { receivedBody += chunk; });
    req.on("end", () => { res.statusCode = 200; res.end("ok"); });
  });

  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
  const address = server.address();
  if (address == null || typeof address === "string") {
    throw new Error("Could not determine test server address");
  }

  try {
    await new PythonBridgeClient(`http://127.0.0.1:${address.port}`, TEST_SHARED_SECRET).deliverEvent(event);
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((err) => (err ? reject(err) : resolve())),
    );
  }

  assert.equal(receivedAuth, `Bearer ${TEST_SHARED_SECRET}`, "must send shared secret");
  assert.equal(receivedDeliveryId, ACCEPT_ACTIVITY_ID, "X-Bridge-Delivery-Id must match delivery_id");

  const parsed = JSON.parse(receivedBody) as FollowAcceptedEvent;
  assert.equal(parsed.event_type, "follow.accepted");
  assert.equal(parsed.object.follow_activity_id, FOLLOW_ACTIVITY_ID);
  assert.equal(parsed.community_actor_id, COMMUNITY_ACTOR_ID);
}

/** Supply the local bridge identity used to check an embedded Follow actor. */
function gatewayConfig(): GatewayConfig {
  return {
    actorIdentifier: "bridge", actorName: "Bridge", actorSummary: "Bridge",
    fedifyOrigin: TEST_ORIGIN, port: 3000,
    pythonBridgeInternalUrl: "http://python-accept.fixture",
    pythonBridgeSharedSecret: TEST_SHARED_SECRET, logLevel: "info",
  };
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
