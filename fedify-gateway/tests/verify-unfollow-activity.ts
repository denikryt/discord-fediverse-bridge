import assert from "node:assert/strict";
import { buildLemmyCompatibleUnfollowActivity, renderPublicActivityJson } from "../src/federation-outbound.js";
import type { GatewayConfig } from "../src/config.js";

/** Build the smallest config object needed by the pure Undo(Follow) builder. */
function makeConfig(fedifyOrigin: string): GatewayConfig {
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

const config = makeConfig("https://bot-test.nachitima.com/");

const actorUri = new URL("https://bot-test.nachitima.com/actors/bridge");
const communityUri = "https://lemmy.example/c/hackers";
const followActivityId = "https://bot-test.nachitima.com/activities/follow/123/abc";

const undo = buildLemmyCompatibleUnfollowActivity(
  config,
  actorUri,
  communityUri,
  followActivityId,
);
const undoJson = await renderPublicActivityJson(undo);
const embeddedFollow = undoJson.object as Record<string, unknown>;

assert.equal(undoJson.type, "Undo");
assert.equal(undoJson.actor, actorUri.href);
assert.deepEqual(undoJson.to, [communityUri]);
assert.equal(embeddedFollow.type, "Follow");
assert.equal(embeddedFollow.id, followActivityId);
assert.equal(embeddedFollow.actor, actorUri.href);
assert.equal(embeddedFollow.object, communityUri);
assert.deepEqual(embeddedFollow.to, [communityUri]);
assert.notEqual(typeof undoJson.object, "string");
assert.match(String(undoJson.id), /^https:\/\/bot-test\.nachitima\.com\/activities\/undo\//);

for (const fedifyOrigin of [
  "https://bot-test.nachitima.com",
  "https://bot-test.nachitima.com/",
]) {
  // Operators commonly configure FEDIFY_ORIGIN with or without a trailing
  // slash. The Undo id must stay under the bridge origin either way so Lemmy's
  // ActivityPub URL-domain verifier accepts the signed Undo(Follow).
  const built = buildLemmyCompatibleUnfollowActivity(
    makeConfig(fedifyOrigin),
    actorUri,
    communityUri,
    followActivityId,
  );
  const builtJson = await renderPublicActivityJson(built);
  assert.match(String(builtJson.id), /^https:\/\/bot-test\.nachitima\.com\/activities\/undo\//);
  assert.ok(
    !String(builtJson.id).includes(".comactivities"),
    "Undo id must not concatenate FEDIFY_ORIGIN and activities without a slash",
  );
}

console.log("verify-unfollow-activity passed");
