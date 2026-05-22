import assert from "node:assert/strict";
import { buildLemmyCompatibleUnfollowActivity, renderPublicActivityJson } from "../src/federation-outbound.js";
import type { GatewayConfig } from "../src/config.js";

const config: GatewayConfig = {
  actorIdentifier: "bridge",
  actorName: "Bridge",
  actorSummary: "Bridge actor",
  bridgePrivateKeyJwkJson: null,
  bridgePublicKeyJwkJson: null,
  communityActorId: null,
  databaseUrl: "sqlite:///../bridge.db",
  fedifyOrigin: "https://bot-test.nachitima.com/",
  port: 3000,
  pythonBridgeEventsUrl: "http://127.0.0.1:8080/internal/activitypub/events",
  pythonBridgeSharedSecret: "secret",
  logLevel: "debug",
};

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

console.log("verify-unfollow-activity passed");
