/**
 * Gateway contract tests for signed local-community relay delivery.
 *
 * Python renders the exact ActivityPub activity and chooses the explicit target
 * inboxes. The gateway must only sign as the local community actor, deliver to
 * those inboxes, and return per-target outcomes.
 */

import assert from "node:assert/strict";

import { sendLocalCommunityRelay } from "../src/federation-outbound.js";
import type { GatewayConfig } from "../src/config.js";
import type { SendLocalCommunityRelayRequest } from "../src/types.js";

const TEST_ORIGIN = "https://discord-bridge.example.com/";

async function main(): Promise<void> {
  await testRelaySendsAlreadyRenderedActivityToExplicitTargets();
  await testRelayRejectsMismatchedActorWithoutDelivery();
  console.log("verify:local-community-relay passed");
}

/**
 * Action: Python asks the gateway to relay an already-rendered Announce to two
 * explicit targets.
 *
 * Expected: the gateway sends the exact activity JSON to both inboxes and
 * reports one successful outcome per target.
 */
async function testRelaySendsAlreadyRenderedActivityToExplicitTargets(): Promise<void> {
  const deliveries: Array<{ sender: unknown; id: string; inboxId: string; payload: unknown }> = [];
  const fakeFederation = {
    createContext() {
      return {
        async sendActivity(
          sender: unknown,
          recipient: { id: URL; inboxId: URL },
          activity: { toJsonLd(): Promise<unknown> },
        ): Promise<void> {
          deliveries.push({
            sender,
            id: recipient.id.href,
            inboxId: recipient.inboxId.href,
            payload: await activity.toJsonLd(),
          });
        },
      };
    },
  };
  const config = buildConfig();
  const activityJson = buildAnnounceJson();
  const request: SendLocalCommunityRelayRequest = {
    signingActorUrl: `${TEST_ORIGIN}communities/hackers`,
    deliveries: [
      {
        deliveryId: 1,
        targetRemoteActorId: "https://lemmy.example/u/alice",
        targetInboxUrl: "https://lemmy.example/u/alice/inbox",
        activityJson,
      },
      {
        deliveryId: 2,
        targetRemoteActorId: "https://lemmy.example/u/carol",
        targetInboxUrl: "https://lemmy.example/u/carol/inbox",
        activityJson,
      },
    ],
  };

  const result = await sendLocalCommunityRelay(fakeFederation as never, config, request);

  assert.deepEqual(result.outcomes.map((outcome) => outcome.ok), [true, true]);
  assert.deepEqual(
    deliveries.map((delivery) => delivery.inboxId).sort(),
    ["https://lemmy.example/u/alice/inbox", "https://lemmy.example/u/carol/inbox"],
  );
  assert.ok(deliveries.every((delivery) => delivery.payload === activityJson));
  assert.ok(deliveries.every((delivery) => {
    const sender = delivery.sender as { username?: string };
    return sender.username === "hackers";
  }));
}

/**
 * Action: Python accidentally passes an activity whose actor does not match the
 * requested signing actor.
 *
 * Expected: the gateway refuses that target before attempting delivery.
 */
async function testRelayRejectsMismatchedActorWithoutDelivery(): Promise<void> {
  const deliveries: unknown[] = [];
  const fakeFederation = {
    createContext() {
      return {
        async sendActivity(): Promise<void> {
          deliveries.push({});
        },
      };
    },
  };
  const activityJson = buildAnnounceJson();
  activityJson.actor = `${TEST_ORIGIN}communities/other`;

  const result = await sendLocalCommunityRelay(fakeFederation as never, buildConfig(), {
    signingActorUrl: `${TEST_ORIGIN}communities/hackers`,
    deliveries: [
      {
        deliveryId: 3,
        targetRemoteActorId: "https://lemmy.example/u/alice",
        targetInboxUrl: "https://lemmy.example/u/alice/inbox",
        activityJson,
      },
    ],
  });

  assert.equal(result.outcomes.length, 1);
  assert.equal(result.outcomes[0]?.ok, false);
  assert.equal(deliveries.length, 0);
}

function buildConfig(): GatewayConfig {
  /** Build the minimal config surface required by the relay helper. */
  return {
    actorIdentifier: "bridge",
    actorName: "Bridge",
    actorSummary: "Bridge summary",
    bridgePrivateKeyJwkJson: null,
    bridgePublicKeyJwkJson: null,
    communityActorId: null,
    databaseUrl: "sqlite:///:memory:",
    fedifyOrigin: TEST_ORIGIN,
    port: 3000,
    pythonBridgeEventsUrl: "http://127.0.0.1:8080/internal/activitypub/events",
    pythonBridgeSharedSecret: "secret",
    logLevel: "info",
  };
}

function buildAnnounceJson(): Record<string, unknown> {
  /** Build the already-rendered relay activity that Python would submit. */
  return {
    "@context": "https://www.w3.org/ns/activitystreams",
    id: `${TEST_ORIGIN}communities/hackers/activities/announce/1`,
    type: "Announce",
    actor: `${TEST_ORIGIN}communities/hackers`,
    object: {
      type: "Create",
      id: "https://lemmy.example/activities/create/post/1",
      actor: "https://lemmy.example/u/bob",
      object: {
        type: "Page",
        id: "https://lemmy.example/post/1",
        attributedTo: "https://lemmy.example/u/bob",
      },
    },
  };
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
