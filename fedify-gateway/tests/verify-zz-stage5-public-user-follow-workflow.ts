/**
 * Public User Actor Follow workflow coverage for Stage 5.
 *
 * These tests drive the production Hono/Fedify app through
 * POST /actors/users/alice/inbox.  Remote actor dereference happens through the
 * normal fetch/document-loader boundary, Python delivery through the internal
 * HTTP bridge boundary, and followers visibility through the public collection
 * route.  The test intentionally does not call buildLocalFollowRequestedEvent()
 * directly or patch Activity.getActor().
 */
import assert from "node:assert/strict";
import { createServer, type Server, type ServerResponse } from "node:http";
import type { Socket } from "node:net";
import { webcrypto } from "node:crypto";

import { signRequest } from "@fedify/fedify";
import { preloadedContexts } from "@fedify/vocab-runtime";

import type { GatewayConfig } from "../src/config.js";
import { createGatewayApp } from "../src/server.js";
import { importFixedRsaKeyPair } from "./support/python-bridge-fixture.js";

const ORIGIN = "https://bridge.example/";
const ALICE_ACTOR = `${ORIGIN}actors/users/alice`;
const BOB_ACTOR = "https://remote.example/users/bob";
const BOB_INBOX = "https://remote.example/users/bob/inbox";
const BOB_SHARED_INBOX = "https://remote.example/inbox";
const MALLORY_ACTOR = "https://remote.example/users/mallory";
const UNSAFE_ACTOR = "https://unsafe.example/users/bob";
const OTHER_ACTOR = "https://other.example/users/mallory";

interface CapturedBridgeEvent {
  event_type?: string;
  actor_id?: string;
  target_actor_kind?: string;
  target_actor_id?: string;
  delivery_id?: string;
  object?: Record<string, unknown>;
}

interface PythonFixture {
  origin: string;
  events: CapturedBridgeEvent[];
  followers: string[];
  close(): Promise<void>;
}

async function main(): Promise<void> {
  const remoteKeys = await importFixedRsaKeyPair();
  const publicKeyPem = await exportPublicKeyPem(remoteKeys.publicKey);
  const privateKeyPem = await exportPrivateKeyPem(remoteKeys.privateKey);
  const restoreFetch = installRemoteActorFetch(publicKeyPem);
  const python = await startRecordingPythonFixture(publicKeyPem, privateKeyPem);
  const app = createGatewayApp(buildConfig(python.origin));

  try {
    await assertUnsignedFollowRejected(app, python);
    await assertSignedFollowReachesPublicListener(app, remoteKeys.privateKey, python);
    await assertSameIdPublicDuplicateDoesNotReachPythonAgain(app, remoteKeys.privateKey, python);
    await assertMismatchedActorDocumentRejected(app, remoteKeys.privateKey, python);
    await assertUnsafeEndpointRejected(app, remoteKeys.privateKey, python);
    await assertUnsignedPublicUndoRejectedWithoutPythonEvent(app, python);
  } finally {
    restoreFetch();
    await python.close();
  }

  console.log("verify:user-actor-public-follow-workflow passed");
}

async function assertUnsignedFollowRejected(
  app: ReturnType<typeof createGatewayApp>,
  python: PythonFixture,
): Promise<void> {
  const response = await app.fetch(new Request(`${ORIGIN}actors/users/alice/inbox`, {
    method: "POST",
    headers: { "Content-Type": "application/activity+json", Accept: "application/activity+json" },
    body: JSON.stringify(buildFollow("https://remote.example/activities/follow/unsigned", BOB_ACTOR)),
  }));
  if (response.status !== 401) {
    throw new Error(`unsigned public Follow expected 401, got ${response.status}: ${await response.text()}`);
  }
  assert.equal(python.events.length, 0, "unsigned public Follow must not emit a Python event");
}

async function assertSignedFollowReachesPublicListener(
  app: ReturnType<typeof createGatewayApp>,
  privateKey: CryptoKey,
  python: PythonFixture,
): Promise<void> {
  const follow = buildFollow("https://remote.example/activities/follow/1", BOB_ACTOR, {
    // Hostile payload fields must be ignored; only the dereferenced actor
    // document may supply inbox/sharedInbox data.
    inbox: "https://attacker.example/not-bobs-inbox",
    endpoints: { sharedInbox: "https://attacker.example/shared" },
  });
  const response = await app.fetch(await signedActivityRequest(follow, privateKey, `${BOB_ACTOR}#main-key`));
  assert.ok([202, 204].includes(response.status), `signed public Follow should be accepted, got ${response.status}`);

  const event = await waitForEvent(python, "local.follow_requested", 1);
  assert.equal(event.target_actor_kind, "user");
  assert.equal(event.target_actor_id, ALICE_ACTOR);
  assert.equal(event.actor_id, BOB_ACTOR);
  assert.equal(event.object?.follow_activity_id, "https://remote.example/activities/follow/1");
  assert.equal(event.object?.remote_inbox_url, BOB_INBOX);
  assert.equal(event.object?.remote_shared_inbox_url, BOB_SHARED_INBOX);
  assert.deepEqual(python.followers, [BOB_ACTOR]);

  const followers = await app.request("/actors/users/alice/followers", {
    headers: { Accept: "application/activity+json" },
  });
  assert.equal(followers.status, 200);
  const body = await followers.json() as Record<string, unknown>;
  assert.equal(body.totalItems, 1);
  assert.equal(body.first, `${ORIGIN}actors/users/alice/followers?page=true`);
}

async function assertSameIdPublicDuplicateDoesNotReachPythonAgain(
  app: ReturnType<typeof createGatewayApp>,
  privateKey: CryptoKey,
  python: PythonFixture,
): Promise<void> {
  const before = python.events.length;
  const follow = buildFollow("https://remote.example/activities/follow/1", BOB_ACTOR);
  const response = await app.fetch(await signedActivityRequest(follow, privateKey, `${BOB_ACTOR}#main-key`));
  assert.ok([202, 204].includes(response.status), `duplicate Follow should be accepted/idempotent, got ${response.status}`);
  await wait(5500);
  assert.equal(
    python.events.length,
    before,
    "Fedify per-inbox idempotency means same-ID public duplicate is not an Accept recovery path",
  );
}

async function assertMismatchedActorDocumentRejected(
  app: ReturnType<typeof createGatewayApp>,
  privateKey: CryptoKey,
  python: PythonFixture,
): Promise<void> {
  const before = python.events.length;
  const follow = buildFollow("https://remote.example/activities/follow/mismatch", MALLORY_ACTOR);
  const response = await app.fetch(await signedActivityRequest(follow, privateKey, `${OTHER_ACTOR}#main-key`));
  assert.ok([202, 204, 401].includes(response.status), `mismatched actor doc should fail closed at public boundary, got ${response.status}`);
  await wait(5500);
  assert.equal(before, python.events.length, "mismatched actor-document id must not emit a Python event");
}

async function assertUnsafeEndpointRejected(
  app: ReturnType<typeof createGatewayApp>,
  privateKey: CryptoKey,
  python: PythonFixture,
): Promise<void> {
  const before = python.events.length;
  const follow = buildFollow("https://unsafe.example/activities/follow/private", UNSAFE_ACTOR);
  const response = await app.fetch(await signedActivityRequest(follow, privateKey, `${UNSAFE_ACTOR}#main-key`));
  assert.ok([202, 204].includes(response.status), `unsafe endpoint should fail closed in listener, got ${response.status}`);
  await wait(5500);
  assert.equal(before, python.events.length, "unsafe dereferenced inbox must not emit a Python event");
}

async function assertUnsignedPublicUndoRejectedWithoutPythonEvent(
  app: ReturnType<typeof createGatewayApp>,
  python: PythonFixture,
): Promise<void> {
  const before = python.events.length;
  const undo = {
    "@context": "https://www.w3.org/ns/activitystreams",
    id: "https://remote.example/activities/undo/1",
    type: "Undo",
    actor: BOB_ACTOR,
    object: buildFollow("https://remote.example/activities/follow/1", BOB_ACTOR),
  };
  const response = await app.fetch(new Request(`${ORIGIN}actors/users/alice/inbox`, {
    method: "POST",
    headers: {
      "Content-Type": "application/activity+json",
      Accept: "application/activity+json",
    },
    body: JSON.stringify(undo),
  }));
  assert.equal(response.status, 401, "unsigned public Undo must reach Fedify auth through the typed inbox, not route-level 404");
  assert.equal(python.events.length, before, "unsigned public Undo must not emit a Python event");

  const followers = await app.request("/actors/users/alice/followers", {
    headers: { Accept: "application/activity+json" },
  });
  assert.equal(followers.status, 200);
  const body = await followers.json() as Record<string, unknown>;
  assert.equal(body.totalItems, 1);
}

function buildFollow(id: string, actor: string, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    "@context": "https://www.w3.org/ns/activitystreams",
    id,
    type: "Follow",
    actor,
    object: ALICE_ACTOR,
    to: ALICE_ACTOR,
    ...extra,
  };
}

async function signedActivityRequest(
  activity: Record<string, unknown>,
  privateKey: CryptoKey,
  keyId: string,
): Promise<Request> {
  const body = JSON.stringify(activity);
  const bodyBytes = new TextEncoder().encode(body);
  return await signRequest(new Request(`${ORIGIN}actors/users/alice/inbox`, {
    method: "POST",
    headers: {
      "Content-Type": "application/activity+json",
      Accept: "application/activity+json",
    },
    body,
  }), privateKey, new URL(keyId), {
    body: bodyBytes.buffer.slice(bodyBytes.byteOffset, bodyBytes.byteOffset + bodyBytes.byteLength),
  });
}

function installRemoteActorFetch(publicKeyPem: string): () => void {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const url = input instanceof URL ? input.href : typeof input === "string" ? input : input.url;
    if (url in preloadedContexts) {
      return new Response(JSON.stringify(preloadedContexts[url]), {
        status: 200,
        headers: { "Content-Type": "application/ld+json" },
      });
    }
    if (url === ALICE_ACTOR) {
      return activityJsonResponse(buildActorDocument(ALICE_ACTOR, `${ALICE_ACTOR}/inbox`, publicKeyPem, null));
    }
    if (url === BOB_ACTOR || url === `${BOB_ACTOR}#main-key`) {
      return activityJsonResponse(buildActorDocument(BOB_ACTOR, BOB_INBOX, publicKeyPem, BOB_SHARED_INBOX));
    }
    if (url === MALLORY_ACTOR || url === `${MALLORY_ACTOR}#main-key` || url === OTHER_ACTOR || url === `${OTHER_ACTOR}#main-key`) {
      return activityJsonResponse(buildActorDocument(OTHER_ACTOR, "https://remote.example/users/mallory/inbox", publicKeyPem, null));
    }
    if (url === UNSAFE_ACTOR || url === `${UNSAFE_ACTOR}#main-key`) {
      return activityJsonResponse(buildActorDocument(UNSAFE_ACTOR, "https://[::ffff:127.0.0.1]/accept-user-actor-follow", publicKeyPem, null));
    }
    return await originalFetch(input, init);
  };
  return () => {
    globalThis.fetch = originalFetch;
  };
}

function buildActorDocument(
  id: string,
  inbox: string,
  publicKeyPem: string,
  sharedInbox: string | null,
  keyOwner: string = id,
): Record<string, unknown> {
  const document: Record<string, unknown> = {
    "@context": ["https://www.w3.org/ns/activitystreams", "https://w3id.org/security/v1"],
    id,
    type: "Person",
    preferredUsername: "bob",
    inbox,
    publicKey: {
      id: `${keyOwner}#main-key`,
      owner: id,
      publicKeyPem,
    },
  };
  if (sharedInbox != null) {
    document.endpoints = { sharedInbox };
  }
  return document;
}

function activityJsonResponse(document: Record<string, unknown>): Response {
  return new Response(JSON.stringify(document), {
    status: 200,
    headers: { "Content-Type": "application/activity+json" },
  });
}

async function startRecordingPythonFixture(publicKeyPem: string, privateKeyPem: string): Promise<PythonFixture> {
  const events: CapturedBridgeEvent[] = [];
  const followers: string[] = [];
  const sockets = new Set<Socket>();
  const server = createServer(async (request, response) => {
    let body = "";
    for await (const chunk of request) body += chunk;
    response.setHeader("Content-Type", "application/json");
    response.setHeader("Cache-Control", "no-store");

    if (request.url === "/internal/activitypub/events" && request.method === "POST") {
      const event = JSON.parse(body) as CapturedBridgeEvent;
      events.push(event);
      if (event.event_type === "local.follow_requested" && event.actor_id != null) {
        if (!followers.includes(event.actor_id)) followers.push(event.actor_id);
      }
      if (event.event_type === "local.unfollow_requested" && event.actor_id != null) {
        const index = followers.indexOf(event.actor_id);
        if (index >= 0) followers.splice(index, 1);
      }
      return json(response, { status: "processed", outcome: "delivered", detail: "fixture" });
    }

    if (request.url === "/internal/fedify/actors/users/alice") {
      return json(response, {
        activitypub_username: "alice",
        actor_url: ALICE_ACTOR,
        inbox_url: `${ALICE_ACTOR}/inbox`,
        outbox_url: `${ALICE_ACTOR}/outbox`,
        followers_url: `${ALICE_ACTOR}/followers`,
        public_key_pem: publicKeyPem,
        private_key_pem: privateKeyPem,
        display_name: "Alice",
        summary: null,
        fields: [],
        avatar: null,
        header: null,
      });
    }

    if (request.url?.startsWith("/internal/fedify/actors/users/alice/followers")) {
      const url = new URL(request.url, "http://python.internal");
      const page = Number(url.searchParams.get("page") ?? "1");
      const pageSize = Number(url.searchParams.get("page_size") ?? "40");
      const start = (page - 1) * pageSize;
      return json(response, {
        followers_url: `${ALICE_ACTOR}/followers`,
        total_items: followers.length,
        page,
        page_size: pageSize,
        actor_ids: followers.slice(start, start + pageSize),
      });
    }

    response.statusCode = 404;
    return json(response, { detail: "not found" });
  });
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.once("close", () => sockets.delete(socket));
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  server.unref();
  const address = server.address();
  if (address == null || typeof address === "string") throw new Error("missing fixture address");
  return {
    origin: `http://127.0.0.1:${address.port}`,
    events,
    followers,
    async close() {
      await new Promise<void>((resolve, reject) => {
        server.close((error) => error == null ? resolve() : reject(error));
        for (const socket of sockets) socket.destroy();
        server.closeAllConnections?.();
      });
    },
  };
}

function json(response: ServerResponse, value: unknown): void {
  response.end(JSON.stringify(value));
}

function buildConfig(pythonBridgeInternalUrl: string): GatewayConfig {
  return {
    actorIdentifier: "bridge",
    actorName: "Bridge",
    actorSummary: "Bridge summary",
    fedifyOrigin: ORIGIN,
    port: 3000,
    pythonBridgeInternalUrl,
    pythonBridgeSharedSecret: "secret",
    logLevel: "info",
  };
}

async function waitForEvent(
  python: PythonFixture,
  eventType: string,
  count: number,
): Promise<CapturedBridgeEvent> {
  const deadline = Date.now() + 6500;
  while (Date.now() < deadline) {
    const matches = python.events.filter((event) => event.event_type === eventType);
    if (matches.length >= count) return matches[count - 1];
    await wait(10);
  }
  throw new Error(`timed out waiting for ${eventType} #${count}; saw ${JSON.stringify(python.events)}`);
}

async function wait(ms: number): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

async function exportPublicKeyPem(publicKey: CryptoKey): Promise<string> {
  return toPem("PUBLIC KEY", Buffer.from(await webcrypto.subtle.exportKey("spki", publicKey)));
}

async function exportPrivateKeyPem(privateKey: CryptoKey): Promise<string> {
  return toPem("PRIVATE KEY", Buffer.from(await webcrypto.subtle.exportKey("pkcs8", privateKey)));
}

function toPem(label: string, bytes: Buffer): string {
  const base64 = bytes.toString("base64");
  const wrapped = base64.match(/.{1,64}/g)?.join("\n") ?? base64;
  return `-----BEGIN ${label}-----\n${wrapped}\n-----END ${label}-----\n`;
}

await main();
process.exit(0);
