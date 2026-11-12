/** NodeBB Group distribution is a verified envelope, not a synthetic Create. */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { buildNodeBBGroupAnnounceEvent } from "../src/federation.js";

const group = "https://nodebb.example/category/42";
const root = "https://nodebb.example/post/101";

async function fixture(name: string): Promise<Record<string, unknown>> {
  /** Read the pinned AP examples shared with the Python behavior suite. */
  return JSON.parse(await readFile(new URL(`../../tests/fixtures/nodebb/${name}`, import.meta.url), "utf8")) as Record<string, unknown>;
}

function wrap(rawRecord: Record<string, unknown>) {
  /** The caller normally obtains this envelope from Fedify's verified typed activity. */
  return { announceId: rawRecord.id as string, rawRecord, rawBodySha256: undefined };
}

const uri = await fixture("announce_root_uri.json");
const event = buildNodeBBGroupAnnounceEvent(wrap(uri));
assert.equal(event?.event_type, "remote.group_announced");
assert.equal(event?.community_actor_id, group);
assert.equal(event?.actor_id, group);
assert.equal(event?.object.ap_id, root);
assert.equal(event?.object.inline_object, null);
assert.equal(event?.delivery_id, uri.id);

const nested = await fixture("announce_nested_create.json");
const nestedEvent = buildNodeBBGroupAnnounceEvent(wrap(nested));
assert.equal(nestedEvent?.delivery_id, nested.id);
assert.equal(nestedEvent?.source_activity_json, nested);
assert.ok(nestedEvent?.object.inline_object);
assert.notEqual(nestedEvent?.object.inner_create_id, nested.id);

// The route hint cannot authorize a different actor or an HTTP/private target.
assert.equal(buildNodeBBGroupAnnounceEvent(wrap({ ...uri, actor: "https://evil.example/users/attacker" })), null);
assert.equal(buildNodeBBGroupAnnounceEvent(wrap({ ...uri, object: "http://127.0.0.1/latest/meta-data" })), null);
assert.equal(buildNodeBBGroupAnnounceEvent(wrap({ ...uri, object: { type: "Delete", id: root } })), null);
console.log("verified NodeBB Group envelope routing passed");
