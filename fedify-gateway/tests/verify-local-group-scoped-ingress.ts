/** Verified local Group destinations are selected from signed address lists and route owner. */
import assert from "node:assert/strict";

import { selectVerifiedLocalGroupDestinations } from "../src/local-group-ingress.js";

const origin = "https://bridge.example";
const groupA = `${origin}/actors/communities/a`;
const groupB = `${origin}/actors/communities/b`;
const person = "https://peer.example/actors/users/creator";
const known = new Set([groupA, groupB]);
const resolve = async (id: string) => known.has(id) ? id : null;
const create = (objectTo: unknown, outerTo: unknown = null) => ({
  type: "Create", id: "https://peer.example/activities/1", actor: person,
  to: outerTo,
  object: { type: "Note", id: "https://peer.example/objects/1", to: objectTo },
});
const shared = { kind: "shared" as const };
const actor = (actorUrl: string) => ({
  kind: "actor" as const, owner: { kind: "community" as const, actorUrl },
});

const both = await selectVerifiedLocalGroupDestinations(origin, shared,
  create([groupA, groupB]), resolve);
assert.deepEqual(both, { kind: "selected", groups: [groupA, groupB] });
const scoped = await selectVerifiedLocalGroupDestinations(origin, actor(groupB),
  create([groupA, groupB]), resolve);
assert.deepEqual(scoped, { kind: "selected", groups: [groupB] });
assert.deepEqual(await selectVerifiedLocalGroupDestinations(origin, actor(groupB),
  create([groupA]), resolve), { kind: "rejected" });
assert.deepEqual(await selectVerifiedLocalGroupDestinations(origin, shared,
  create([groupA, `${origin}/actors/communities/missing`]), resolve), { kind: "rejected" });
assert.deepEqual(await selectVerifiedLocalGroupDestinations(origin, shared,
  create(["https://foreign.example/c/1"]), resolve), { kind: "remote" });
assert.deepEqual(await selectVerifiedLocalGroupDestinations(origin, shared,
  create([groupA], [groupB]), resolve), { kind: "selected", groups: [groupA, groupB] });
assert.deepEqual(await selectVerifiedLocalGroupDestinations(origin, { kind: "unknown" },
  create([groupA]), resolve), { kind: "rejected" });
assert.deepEqual(await selectVerifiedLocalGroupDestinations(origin, shared,
  create(Array.from({ length: 26 }, (_, i) => `${origin}/actors/communities/${i}`)),
  resolve), { kind: "rejected" });
const announce = {
  type: "Announce", id: "https://peer.example/announces/outer", actor: person,
  object: create([groupA, groupB]),
};
assert.deepEqual(await selectVerifiedLocalGroupDestinations(origin, shared,
  announce, resolve), { kind: "selected", groups: [groupA, groupB] });
// A verified Group Announce(Update(Note)) may address only in the inner Note.
const announcedUpdate = {
  type: "Announce", id: "https://peer.example/announce/update/1", actor: person,
  object: {
    type: "Update", actor: person, id: "https://peer.example/updates/1",
    object: { type: "Note", id: "https://peer.example/objects/1", to: groupA },
  },
};
assert.deepEqual(await selectVerifiedLocalGroupDestinations(origin, shared,
  announcedUpdate, resolve), { kind: "selected", groups: [groupA] });
console.log("verified local Group ingress selection passed");
