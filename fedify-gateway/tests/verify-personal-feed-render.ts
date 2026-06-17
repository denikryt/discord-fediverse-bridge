import assert from "node:assert/strict";

import { renderPersonalFeedCreate } from "../src/federation-outbound.js";
import { buildPublishedCreateActivityJson } from "../src/published-objects.js";

const config = { fedifyOrigin: "https://bridge.test" } as never;
const operationId = "a".repeat(64);
const activityId = `https://bridge.test/users/alice/activities/create/post/${operationId}`;
const objectId = `https://bridge.test/users/alice/post/${operationId}`;
const rendered = renderPersonalFeedCreate(config, {
  actorUsername: "alice",
  operationId,
  activityId,
  objectId,
  bodyMarkdown: "hello <script>alert(1)</script>",
  publishedAt: "2026-06-17T12:34:56.789Z",
});
assert.equal(rendered.activityId, activityId);
assert.equal((rendered.activityJson.object as Record<string, unknown>).type, "Note");
assert.equal((rendered.activityJson.object as Record<string, unknown>).published, rendered.publishedAt);
assert.match(String((rendered.activityJson.object as Record<string, unknown>).content), /&lt;script&gt;/);
assert.equal("audience" in rendered.activityJson, false);

const reconstructed = buildPublishedCreateActivityJson({
  actorUsername: "alice",
  actorUrl: "https://bridge.test/actors/alice",
  publicationContext: "personal_feed",
  communityActorUrl: null,
  activityId,
  objectId,
  kind: "post",
  title: null,
  bodyMarkdown: "hello <script>alert(1)</script>",
  inReplyToObjectId: null,
  publishedAt: "2026-06-17T12:34:56.789Z",
  discordGuildId: 100,
  discordChannelId: 200,
  discordMessageId: 300,
});
assert.deepEqual(reconstructed, rendered.activityJson);
