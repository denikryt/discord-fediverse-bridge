/**
 * Regression tests for bridge-owned URL construction from FEDIFY_ORIGIN.
 *
 * Operators may configure FEDIFY_ORIGIN with or without a trailing slash. The
 * gateway must still produce actor, object, and activity ids under the same
 * bridge host instead of accidentally concatenating host and path text.
 */

import assert from "node:assert/strict";

import type { GatewayConfig } from "../src/config.js";
import { buildGatewayUrl } from "../src/federation-outbound.js";

function makeConfig(fedifyOrigin: string): GatewayConfig {
  /** Build the smallest config object needed for pure URL-construction tests. */
  return {
    actorIdentifier: "bridge",
    actorName: "Bridge",
    actorSummary: "Bridge actor",
    bridgePrivateKeyJwkJson: null,
    bridgePublicKeyJwkJson: null,
    databaseUrl: "sqlite:///../bridge.db",
    fedifyOrigin,
    port: 3000,
    pythonBridgeEventsUrl: "http://127.0.0.1:8080/internal/activitypub/events",
    pythonBridgeSharedSecret: "secret",
    logLevel: "debug",
  };
}

for (const fedifyOrigin of [
  "https://bot-test.nachitima.com",
  "https://bot-test.nachitima.com/",
]) {
  // Activity paths are the failure-sensitive case: Lemmy verifies that
  // bridge-owned ids remain on the bridge domain before accepting Undo(Follow).
  assert.equal(
    buildGatewayUrl(makeConfig(fedifyOrigin), "activities/undo/123/abc").href,
    "https://bot-test.nachitima.com/activities/undo/123/abc",
  );
  assert.equal(
    buildGatewayUrl(makeConfig(fedifyOrigin), "/activities/follow/123/abc").href,
    "https://bot-test.nachitima.com/activities/follow/123/abc",
  );
  assert.ok(
    !buildGatewayUrl(makeConfig(fedifyOrigin), "activities/undo/123/abc").href.includes(".comactivities"),
    "bridge URL builder must not concatenate origin host and path without a slash",
  );
}

console.log("verify-gateway-url passed");
