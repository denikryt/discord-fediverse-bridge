/**
 * Verifies Stage 3 typed actor route-tail parsing without production cutover.
 *
 * The parser receives Fedify reserved-expansion identifier tails such as
 * `users/alice`. These are not full URLs and must be resolved by kind before
 * any Stage 4 actor-store, key-pair, or inbox-owner lookup happens.
 */
import assert from "node:assert/strict";

import {
  parseTypedActorIdentifier,
  typedActorIdentifierTail,
  type TypedActorKind,
} from "../src/typed-actor-routing.js";

/** Exercise accepted service, user, and community tails plus malformed input. */
async function main(): Promise<void> {
  assert.deepEqual(parseTypedActorIdentifier("users/alice"), {
    kind: "user",
    identifier: "alice",
    tail: "users/alice",
  });
  assert.deepEqual(parseTypedActorIdentifier("communities/python"), {
    kind: "community",
    identifier: "python",
    tail: "communities/python",
  });
  assert.deepEqual(parseTypedActorIdentifier("services/bridge"), {
    kind: "service",
    identifier: "bridge",
    tail: "services/bridge",
  });

  for (const tail of [
    "alice",
    "actors/users/alice",
    "user/alice",
    "users/",
    "users/alice/inbox",
    "communities",
    "services/bridge/extra",
    "moderators/alice",
    "users/alice%2Fbob",
    "users/Alice",
    "communities/bad/slug",
    "services/bad/bridge",
    "services/",
    "/users/alice",
    "users/alice?x=1",
    "users/alice#main-key",
  ]) {
    assert.equal(parseTypedActorIdentifier(tail), null, `tail must reject: ${tail}`);
  }

  assert.equal(typedActorIdentifierTail("user", "alice"), "users/alice");
  assert.equal(typedActorIdentifierTail("community", "python"), "communities/python");
  assert.equal(typedActorIdentifierTail("service", "bridge"), "services/bridge");

  for (const [kind, identifier] of [
    ["user", "Alice"],
    ["community", "bad/slug"],
    ["service", "bad/bridge"],
  ] as Array<[TypedActorKind, string]>) {
    assert.throws(() => typedActorIdentifierTail(kind, identifier));
  }

  console.log("verify:typed-actor-routing passed");
}

await main();
