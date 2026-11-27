/** Exact remote Group addressing for a direct NodeBB Delete, including JSON-LD ID objects. */
import assert from "node:assert/strict";
import { normalizeDeleteActivityFromJson, normalizeVerifiedAnnounceDeleteFromJson, resolveCommunityActorIdForDelete } from "../src/normalize.js";

const group = "https://nodebb.example/category/12";
const author = "https://nodebb.example/uid/2";
const comment = "https://nodebb.example/post/79";

async function main(): Promise<void> {
  /** NodeBB may encode an IRI as a JSON-LD node; it is not a Person actor. */
  const input = {
    type: "Delete", id: "https://nodebb.example/activities/delete-79", actor: author,
    object: comment, audience: { id: group },
  };
  assert.equal(resolveCommunityActorIdForDelete(input), group);
  const routed = await normalizeDeleteActivityFromJson(input);
  assert.ok(routed != null && "community_actor_id" in routed);
  const event = routed as {community_actor_id: string; event_type: string; object: {ap_id: string}};
  assert.equal(event.community_actor_id, group);
  assert.equal(event.event_type, "post.deleted"); // SQLite remaps the /post/ reply to comment.
  assert.equal(event.object.ap_id, comment);

  // An array of JSON-LD nodes is equally legitimate, and a foreign Group
  // cannot be inferred from the author's URL or an unrelated Note body.
  const viaCc = { ...input, audience: undefined, cc: [{ id: group }] };
  assert.equal(resolveCommunityActorIdForDelete(viaCc), group);
  assert.equal(resolveCommunityActorIdForDelete({ ...input, audience: { id: author } }), null);
  assert.equal(resolveCommunityActorIdForDelete({ ...input, audience: { id: "not a URI" } }), null);

  // The existing Lemmy/plain-IRI path remains identical, without an unscoped
  // object lookup, prefix comparison or synthetic Person authorization.
  assert.equal(resolveCommunityActorIdForDelete({ ...input, audience: group }), group);
  assert.equal(resolveCommunityActorIdForDelete({ ...input, audience: [group] }), group);

  // NodeBB's nested Delete can omit Group addressing entirely. The verified
  // outer distributor supplies the scope, never the Person actor or a global
  // Note lookup. The original inner activity is not rewritten.
  const inner = { ...input, audience: undefined };
  const envelope = { type: "Announce", id: group + "/announce/delete-79",
    actor: group, audience: { id: group }, object: inner };
  const wrapped = await normalizeVerifiedAnnounceDeleteFromJson(envelope);
  assert.ok(wrapped != null && "community_actor_id" in wrapped);
  assert.equal(wrapped.community_actor_id, group);
  assert.equal(wrapped.actor_id, author);
  assert.equal(wrapped.object.ap_id, comment);
  assert.equal(wrapped.event_type, "post.deleted"); // Python resolves saved comment kind.
  assert.equal(inner.audience, undefined);

  // Even if the same object is known in a Personal Feed, Group-distributed
  // Delete must not select that independent conversation or query it.
  let personalLookups = 0;
  const client = {
    async loadMessageMappingByObjectId() { return null; },
    async loadPublishedActivityObjectByObjectId() { return null; },
    async loadConversationTargetByObjectId() {
      personalLookups++;
      return { publicationContext: "personal_feed" as const, rootObjectId: comment, actorId: author };
    },
  };
  const collision = await normalizeVerifiedAnnounceDeleteFromJson({ ...envelope, actor: { id: group } }, { pythonBridgeClient: client });
  assert.ok(collision != null && "community_actor_id" in collision);
  assert.equal(collision.community_actor_id, group);
  assert.equal(collision.target?.kind, undefined);
  assert.equal(personalLookups, 0);
  const personal = await normalizeDeleteActivityFromJson(inner, { pythonBridgeClient: client });
  assert.ok(personal != null && "community_actor_id" in personal);
  assert.equal(personal.target?.kind, "personal_feed");
  assert.equal(personalLookups, 1);

  // Direct Delete remains unchanged. A Person-signed Announce cannot gain
  // Group scope merely by naming a Group as a recipient.
  const direct = await normalizeDeleteActivityFromJson(inner);
  assert.ok(direct != null && "community_actor_id" in direct);
  assert.equal(direct.community_actor_id, author);
  const personWrapper = await normalizeVerifiedAnnounceDeleteFromJson({ ...envelope, actor: author });
  assert.ok(personWrapper != null && "community_actor_id" in personWrapper);
  assert.equal(personWrapper.community_actor_id, author);

  // Explicit nested scope is never silently rewritten to match the outer
  // signer. Python will reject a mismatched signer for a remote Group view.
  const other = "https://nodebb.example/category/99";
  const conflict = await normalizeVerifiedAnnounceDeleteFromJson({
    ...envelope, object: { ...inner, audience: other },
  });
  assert.ok(conflict != null && "community_actor_id" in conflict);
  assert.equal(conflict.community_actor_id, other);
}

await main();
console.log("verify:cp36-community-delete-routing passed");
