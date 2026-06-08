import assert from "node:assert/strict";

import { followCommunity } from "../src/federation-outbound.js";
import type { GatewayConfig } from "../src/config.js";

const TEST_ORIGIN = "https://bot-test.nachitima.com";
const COMMUNITY_ACTOR_URL = "https://lemmy.example/c/hackers";
const COMMUNITY_INBOX_URL = "https://lemmy.example/c/hackers/inbox";

const config: GatewayConfig = {
  actorIdentifier: "bridge",
  actorName: "Bridge",
  actorSummary: "Bridge actor",
  pythonBridgeInternalUrl: "http://127.0.0.1:1",
  fedifyOrigin: TEST_ORIGIN,
  port: 3000,
    pythonBridgeSharedSecret: "secret",
  logLevel: "info",
};

async function main(): Promise<void> {
  /** Stage regression: Follow ids must remain valid without trailing slash. */
  const originalFetch = globalThis.fetch;
  const sentActivities: Array<{ recipients: Array<{ id: string; inboxId: string }>; activity: { id?: URL } }> = [];

  globalThis.fetch = (async () =>
    new Response(
      JSON.stringify({
        id: COMMUNITY_ACTOR_URL,
        inbox: COMMUNITY_INBOX_URL,
      }),
      {
        status: 200,
        headers: { "content-type": "application/activity+json" },
      },
    )) as typeof fetch;

  try {
    const fakeFederation = {
      createContext() {
        return {
          getActorUri() {
            return new URL(`${TEST_ORIGIN}/actors/bridge`);
          },
          async sendActivity(
            _sender: object,
            recipient: { id: URL; inboxId: URL },
            activity: { id?: URL },
          ): Promise<void> {
            sentActivities.push({
              recipients: [{ id: recipient.id.href, inboxId: recipient.inboxId.href }],
              activity,
            });
          },
        };
      },
    };

    const result = await followCommunity(
      fakeFederation as never,
      config,
      COMMUNITY_ACTOR_URL,
    );

    assert.match(
      result.followActivityId,
      /^https:\/\/bot-test\.nachitima\.com\/activities\/follow\/\d+\/[a-z0-9]+$/,
    );
    assert.equal(sentActivities.length, 1);
    assert.equal(
      sentActivities[0]?.activity.id?.href,
      result.followActivityId,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }

  console.log("verify-follow-activity-id passed");
}

await main();
