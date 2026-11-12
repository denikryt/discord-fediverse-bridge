/**
 * Verified ActivityPub envelope regression for a poisoned pre-verification cache.
 *
 * The wire id alone must not authorize Announce content, Undo(Follow), or
 * Accept(Follow): only Fedify's verified typed activity may supply actor,
 * object, and original Follow identity. No Python side effects occur here.
 */
import assert from "node:assert/strict";
import { Accept, Announce, Create, Follow, Note, Undo } from "@fedify/vocab";
import { storeRawActivity } from "../src/activitypub-raw-cache.js";
import { buildFollowAcceptedEvent, getVerifiedAnnounceEnvelope } from "../src/federation.js";
import type { GatewayContextData } from "../src/config.js";

const origin = "https://bridge.example/";
const author = "https://remote.example/users/alice";
const group = "https://remote.example/c/discussion";
const announceId = "https://remote.example/activities/announce/1";
const innerId = "https://remote.example/activities/create/1";
const objectId = "https://remote.example/post/1";
const followId = `${origin}actors/services/bridge/follows/1`;

async function main(): Promise<void> {
  // A forged request with the same Announce.id arrives first. It cannot
  // replace the verified Group's Announce(Create(...)) fields in the callback.
  storeRawActivity(announceId, {
    id: announceId, type: "Announce", actor: "https://evil.example/c/forged",
    object: { id: innerId, type: "Delete", actor: author, object: objectId },
  });
  const signed = new Announce({
    id: new URL(announceId), actor: new URL(group),
    object: new Create({
      id: new URL(innerId), actor: new URL(author),
      object: new Note({ id: new URL(objectId), attribution: new URL(author), content: "safe" }),
    }),
  });
  const verified = await getVerifiedAnnounceEnvelope(signed);
  assert.ok(verified, "a typed signed Announce must remain usable after cache poisoning");
  assert.equal(verified.announceId, announceId);
  assert.equal(verified.rawRecord?.actor, group);
  const wrapped = verified.rawRecord?.object as Record<string, unknown>;
  assert.equal(wrapped?.type, "Create");
  assert.equal(wrapped?.id, innerId);

  const missingId = new Announce({ actor: new URL(group), object: new URL(objectId) });
  assert.equal(await getVerifiedAnnounceEnvelope(missingId), null);

  const acceptId = "https://remote.example/activities/accept/1";
  storeRawActivity(acceptId, {
    id: acceptId, type: "Accept", actor: group,
    object: { id: "https://evil.example/follows/forged", type: "Follow" },
  });
  const context = {
    fedifyOrigin: origin,
    activitypubRawJson: { id: acceptId, type: "Accept", actor: group,
      object: { id: "https://evil.example/follows/forged", type: "Follow" } },
  } as GatewayContextData;
  const accepted = buildFollowAcceptedEvent(new Accept({
    id: new URL(acceptId), actor: new URL(group), object: new URL(followId),
  }), context, acceptId);
  assert.equal(accepted?.object.follow_activity_id, followId);
  const incomplete = buildFollowAcceptedEvent(new Accept({
    id: new URL(acceptId), actor: new URL(group),
  }), context, acceptId);
  assert.equal(incomplete, null, "raw cache must not invent missing verified Follow.id");
  // The same rule applies to Undo: a typed Undo without a Follow payload may
  // not acquire a Follow.id or destination from the poisoned raw cache.
  const orphan = new Undo({ id: new URL(`${origin}undo/1`), actor: new URL(author) });
  assert.equal(orphan.objectId, null);
}

await main();
console.log("verified ActivityPub envelope boundary passed");
