/**
 * Verifies a fixture-only Fedify reserved-expansion route shape for Stage 4.
 *
 * This does not exercise production `createGatewayFederation()`. It proves that
 * a slash-containing `{+identifier}` value can reach the Stage 3 parser before
 * later actor lookup, key ownership, and inbox-owner resolution are wired.
 */
import assert from "node:assert/strict";

import { createTypedFederationFixture } from "./support/typed-federation-fixture.js";

const ORIGIN = "https://bridge.example";

/** Exercise typed actor dispatcher paths and unsigned inbox route matching. */
async function main(): Promise<void> {
  const fixture = createTypedFederationFixture(ORIGIN);

  for (const [path, expectedType] of [
    ["/actors/users/alice", "Person"],
    ["/actors/communities/python", "Group"],
    ["/actors/services/bridge", "Application"],
  ] as Array<[string, string]>) {
    const response = await fixture.federation.fetch(new Request(`${ORIGIN}${path}`, {
      headers: { Accept: "application/activity+json" },
    }), { contextData: undefined });
    assert.equal(response.status, 200, `${path} should dispatch in the fixture`);
    const actor = await response.json() as Record<string, unknown>;
    assert.equal(actor.type, expectedType);
    assert.equal(actor.id, `${ORIGIN}${path}`);
  }

  assert.deepEqual(fixture.actorDispatches.map((actor) => actor.tail), [
    "users/alice",
    "communities/python",
    "services/bridge",
  ]);

  const malformed = await fixture.federation.fetch(new Request(`${ORIGIN}/actors/users/alice/inbox`, {
    headers: { Accept: "application/activity+json" },
  }), { contextData: undefined });
  assert.equal(malformed.status, 404, "GET actor dispatcher must not treat inbox as an actor tail");

  const unsignedValidInbox = await fixture.federation.fetch(new Request(`${ORIGIN}/actors/users/alice/inbox`, {
    method: "POST",
    headers: { "Content-Type": "application/activity+json", Accept: "application/activity+json" },
    body: JSON.stringify({
      "@context": "https://www.w3.org/ns/activitystreams",
      id: "https://remote.example/activities/create/1",
      type: "Create",
      actor: "https://remote.example/users/bob",
    }),
  }), { contextData: undefined });
  assert.equal(
    unsignedValidInbox.status,
    401,
    "valid typed actor inbox reaches Fedify auth instead of route-level 404",
  );

  const invalidInbox = await fixture.federation.fetch(new Request(`${ORIGIN}/actors/user/alice/inbox`, {
    method: "POST",
    headers: { "Content-Type": "application/activity+json", Accept: "application/activity+json" },
    body: "{}",
  }), { contextData: undefined });
  assert.equal(invalidInbox.status, 404, "invalid typed inbox tail must fail closed");

  console.log("verify:gateway-typed-route-fixture passed");
}

await main();
