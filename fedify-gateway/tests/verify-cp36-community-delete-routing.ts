/** Exact remote Group addressing for a direct NodeBB Delete, including JSON-LD ID objects. */
import assert from "node:assert/strict";
import { normalizeDeleteActivityFromJson, resolveCommunityActorIdForDelete } from "../src/normalize.js";

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
}

await main();
console.log("verify:cp36-community-delete-routing passed");
