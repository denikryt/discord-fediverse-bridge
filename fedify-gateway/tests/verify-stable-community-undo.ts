/**
 * The last remote Group unsubscribed channel must retain one Undo(Follow)
 * identity even after retry/restart; a different Follow receives another ID.
 */
import assert from "node:assert/strict";
import type { GatewayConfig } from "../src/config.js";
import { buildLemmyCompatibleUnfollowActivity } from "../src/outbound/community-follow.js";

const config = {
  fedifyOrigin: "https://bridge.example",
  actorIdentifier: "bridge",
} as GatewayConfig;
const actor = new URL("https://bridge.example/actors/services/bridge");
const group = "https://nodebb.example/category/42";
const follow = "https://bridge.example/actors/services/bridge/activities/follow/fixed";

const first = buildLemmyCompatibleUnfollowActivity(config, actor, group, follow);
const retry = buildLemmyCompatibleUnfollowActivity(config, actor, group, follow);
const next = buildLemmyCompatibleUnfollowActivity(config, actor, group, follow + "-next");
assert.equal(first.id?.href, retry.id?.href, "repeated Undo must preserve the ID");
assert.notEqual(first.id?.href, next.id?.href, "different Follow must have a distinct Undo ID");
assert.equal(first.actorId?.href, actor.href);
assert.equal(first.objectId?.href, follow);
console.log("Stable Group Undo(Follow) identity: PASS");
