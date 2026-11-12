/** NodeBB same-wire replay reaches fanout, without weakening unrelated dedup. */
import assert from "node:assert/strict";
import { gatewayInboxIdempotencyKey } from "../src/inbox-idempotency.js";

const group = new URL("https://nodebb.example/category/42");
const activity = { id: new URL("https://nodebb.example/announces/1"), actorId: group };
const shared = { origin: new URL("https://bridge.example"), recipient: null };
const actorInbox = { origin: new URL("https://bridge.example"), recipient: "communities/test" };

assert.equal(gatewayInboxIdempotencyKey(shared, activity, true), null);
assert.equal(gatewayInboxIdempotencyKey(actorInbox, activity, true), null);
// Follow/Undo, personal content and non-NodeBB announcements keep per-inbox dedup.
const key = gatewayInboxIdempotencyKey(shared, activity, false);
assert.equal(typeof key, "string");
assert.notEqual(key, gatewayInboxIdempotencyKey(actorInbox, activity, false));
assert.equal(key, gatewayInboxIdempotencyKey(shared, activity, false));
assert.notEqual(gatewayInboxIdempotencyKey(shared, {
  ...activity, actorId: new URL("https://nodebb.example/actors/users/person"),
}, true), null);
assert.notEqual(gatewayInboxIdempotencyKey(shared, {
  ...activity, actorId: new URL("https://nodebb.example/category/42?spoof=1"),
}, true), null);
assert.notEqual(gatewayInboxIdempotencyKey(shared, {
  ...activity, actorId: new URL("http://nodebb.example/category/42"),
}, true), null);
assert.equal(gatewayInboxIdempotencyKey(shared, { ...activity, id: null }, true), null);
console.log("NodeBB inbox idempotency boundary passed");
