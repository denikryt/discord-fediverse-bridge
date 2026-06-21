/**
 * Verifies remote inbox admission before User Actor Accept/fanout use.
 *
 * Actor-document inbox URLs are protocol data, but they are still remote input.
 * The gateway must reject local/private/credentialed/non-HTTPS endpoints while
 * keeping a narrow HTTP loopback override for outer-boundary test fixtures.
 */
import assert from "node:assert/strict";

import { admitRemoteDeliveryEndpoint } from "../src/remote-delivery-endpoints.js";

/** Exercise the production and fixture-only endpoint policy. */
function main(): void {
  assert.equal(
    admitRemoteDeliveryEndpoint("https://remote.example/users/bob/inbox", {
      localOrigin: "https://bridge.example",
    }),
    "https://remote.example/users/bob/inbox",
  );

  for (const endpoint of [
    "http://remote.example/users/bob/inbox",
    "https://user:pass@remote.example/inbox",
    "https://remote.example/inbox#frag",
    "http://127.0.0.1:3000/accept-user-actor-follow",
    "https://127.0.0.1/inbox",
    "https://169.254.169.254/latest/meta-data",
    "https://10.0.0.1/inbox",
    "https://bridge.example/accept-user-actor-follow",
  ]) {
    assert.throws(
      () => admitRemoteDeliveryEndpoint(endpoint, { localOrigin: "https://bridge.example" }),
      /remote delivery endpoint/,
      `${endpoint} must be rejected`,
    );
  }

  assert.equal(
    admitRemoteDeliveryEndpoint("http://127.0.0.1:3000/users/bob/inbox", {
      localOrigin: "https://bridge.example",
      allowHttpLoopbackForTests: true,
    }),
    "http://127.0.0.1:3000/users/bob/inbox",
  );

  console.log("verify:remote-delivery-endpoint-admission passed");
}

main();
