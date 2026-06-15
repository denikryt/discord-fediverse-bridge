import assert from "node:assert/strict";

import { Image, PropertyValue } from "@fedify/vocab";

import { buildUserPersonActor } from "../src/actors.js";
import type { UserActorIdentity } from "../src/actor-store.js";

const identity: UserActorIdentity = {
  actorId: new URL("https://bridge.example/actors/alice"),
  followersId: new URL("https://bridge.example/actors/alice/followers"),
  inboxId: new URL("https://bridge.example/actors/alice/inbox"),
  outboxId: new URL("https://bridge.example/actors/alice/outbox"),
  publicKeyPem: "public",
  privateKeyPem: "private",
  username: "alice",
  displayName: "Alice <Admin>",
  summary: "Hello <script>\nWorld & friends",
  fields: [{ name: `Research & Development <"R&D">`, value: "<b>https://example.com</b>" }],
  avatar: { url: new URL("https://bridge.example/media/profile/avatar"), contentType: "image/png", width: 400, height: 400 },
  header: { url: new URL("https://bridge.example/media/profile/header"), contentType: "image/png", width: 1500, height: 500 },
};

const actor = buildUserPersonActor(identity, new URL("https://bridge.example/inbox"), []);
assert.equal(actor.preferredUsername?.toString(), "alice");
assert.equal(actor.name?.toString(), "Alice <Admin>");
assert.equal(actor.summary?.toString(), "Hello &lt;script&gt;<br>World &amp; friends");
const icon = await actor.getIcon();
const image = await actor.getImage();
const attachments = [];
for await (const attachment of actor.getAttachments()) attachments.push(attachment);
assert.ok(icon instanceof Image);
assert.ok(image instanceof Image);
assert.equal(icon?.url?.href, identity.avatar?.url.href);
assert.equal(image?.url?.href, identity.header?.url.href);
assert.equal(attachments.length, 1);
assert.ok(attachments[0] instanceof PropertyValue);
const property = attachments[0] as PropertyValue;
assert.equal(property.name?.toString(), `Research & Development <"R&D">`);
assert.equal(property.value?.toString(), "&lt;b&gt;https://example.com&lt;/b&gt;");
const jsonLd = await actor.toJsonLd() as Record<string, unknown>;
const serializedAttachment = jsonLd.attachment as Record<string, unknown>;
assert.equal(serializedAttachment.name, `Research & Development <"R&D">`);

const defaultIdentity: UserActorIdentity = {
  ...identity,
  displayName: "alice",
  summary: null,
  fields: [],
  avatar: null,
  header: null,
};
const defaultActor = buildUserPersonActor(
  defaultIdentity,
  new URL("https://bridge.example/inbox"),
  [],
);
const defaultJsonLd = await defaultActor.toJsonLd() as Record<string, unknown>;
assert.equal(defaultActor.name?.toString(), "alice");
assert.equal(defaultActor.summary, null);
assert.equal(defaultJsonLd.icon, undefined);
assert.equal(defaultJsonLd.image, undefined);
assert.equal(defaultJsonLd.attachment, undefined);

console.log("verify:user-actor-profile passed");
