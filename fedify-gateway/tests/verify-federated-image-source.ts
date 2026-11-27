/** A Community projection must retain image-bearing ActivityPub source fields. */
import assert from "node:assert/strict";
import { normalizeCreateActivityFromJson, normalizeUpdateActivityFromJson } from "../src/normalize.js";

const group = "https://nodebb.example/category/2";
const actor = "https://nodebb.example/uid/2";
const id = "https://nodebb.example/post/91";
const image = "https://nodebb.example/assets/uploads/photo.png";
const object = {
  type: "Note", id, attributedTo: actor, audience: group,
  content: `<p>Topic</p><img src="${image}">`,
  source: { content: `![photo](${image})`, mediaType: "text/markdown" },
  attachment: [{ type: "Image", mediaType: "image/png", url: image }],
  published: "2026-11-26T12:00:00Z",
};
const create = { type: "Create", id: id + "/create", actor, object };
const created = await normalizeCreateActivityFromJson(create);
assert.ok(created);
assert.equal(created.community_actor_id, group);
assert.equal(created.object.ap_id, id);
assert.equal(created.object.body_markdown, object.source.content);
assert.deepEqual(created.object.media_source, { content: object.content, attachment: object.attachment });

// An Update is a replacement projection, not a merge with the old attachment.
const updated = await normalizeUpdateActivityFromJson({ ...create, type: "Update", id: id + "/update",
  object: { ...object, source: undefined, content: "<p>Image removed</p>", attachment: [] } });
assert.ok(updated && "community_actor_id" in updated);
assert.equal(updated.object.ap_id, id);
assert.equal(updated.object.body_markdown, "Image removed");
assert.deepEqual(updated.object.media_source, { attachment: [] });

// A Page with an external article link keeps the same canonical identity;
// Python inspects MIME and extension, rather than fetching every hyperlink.
const page = await normalizeCreateActivityFromJson({ ...create, object: { ...object,
  type: "Page", name: "Title", attachment: [{ type: "Link", href: "https://example.org/article" }] } });
assert.ok(page);
assert.equal(page.object.url, "https://example.org/article");
assert.equal(page.object.ap_id, id);
console.log("federated image source normalization passed");
