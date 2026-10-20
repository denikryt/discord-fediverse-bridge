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
  /** Regression: Follow ids must remain valid without trailing slash. */
  const sentActivities: Array<{ recipients: Array<{ id: string; inboxId: string }>; activity: { id?: URL } }> = [];

  // The production resolver uses Fedify Context.lookupObject rather than the
  // global fetch stub; model that exact trust boundary in this fixture.
  const fakeFederation = {
    createContext() {
      return {
        getActorUri(identifier: string) {
          assert.equal(identifier, "services/bridge");
          return new URL(`${TEST_ORIGIN}/actors/services/bridge`);
        },
        async lookupObject(requested: string, options?: { crossOrigin?: string }) {
          assert.equal(requested, COMMUNITY_ACTOR_URL);
          assert.deepEqual(options, { crossOrigin: "throw" });
          return { id: new URL(COMMUNITY_ACTOR_URL), inboxId: new URL(COMMUNITY_INBOX_URL) };
        },
        async sendActivity(
          sender: { username?: string },
          recipient: { id: URL; inboxId: URL },
          activity: { id?: URL },
        ): Promise<void> {
          assert.equal(sender.username, "services/bridge");
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
    /^https:\/\/bot-test\.nachitima\.com\/actors\/services\/bridge\/activities\/follow\/\d+-[a-z0-9]+$/,
  );
  assert.equal(sentActivities.length, 1);
  assert.equal(
    sentActivities[0]?.activity.id?.href,
    result.followActivityId,
  );

  console.log("verify-follow-activity-id passed");
}

await main();
