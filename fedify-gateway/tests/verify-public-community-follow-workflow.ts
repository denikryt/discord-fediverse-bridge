/**
 * Public local-community Group Follow workflow coverage for personal feed control plane.
 *
 * The scenarios drive the production Hono/Fedify app through typed community
 * inboxes and the shared inbox. Remote actor documents are served at the fetch
 * boundary, and Python side effects are captured through the internal bridge
 * HTTP boundary so route-owner binding and public followers visibility are
 * tested through the same wiring used in deployment.
 */
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";

import { signRequest } from "@fedify/fedify";
import { preloadedContexts } from "@fedify/vocab-runtime";

import type { GatewayConfig } from "../src/config.js";
import { createGatewayApp } from "../src/server.js";
import { importFixedRsaKeyPair } from "./support/python-bridge-fixture.js";

const ORIGIN = "https://bridge.example/";
const PYTHON_ACTOR = `${ORIGIN}actors/communities/python`;
const RUBY_ACTOR = `${ORIGIN}actors/communities/ruby`;
const BOB_ACTOR = "https://remote.example/u/bob";
const BOB_INBOX = "https://remote.example/u/bob/inbox";
const BOB_SHARED_INBOX = "https://remote.example/inbox";
const MISMATCH_DOC_ACTOR = "https://mismatch.example/u/bob";
const UNSAFE_ACTOR = "https://unsafe.example/u/bob";
const OTHER_ACTOR = "https://other.example/u/mallory";
let pythonFixtureCounter = 0;

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
  fetch(url: string, init?: RequestInit): Promise<Response | null>;
  close(): Promise<void>;
}

interface PublicWorkflowFixtureContext {
  app: ReturnType<typeof createGatewayApp>;
  privateKey: CryptoKey;
  python: PythonFixture;
}

async function main(): Promise<void> {
  await withPublicWorkflowFixture(async ({ app, privateKey, python }) => {
    await assertUnsignedFollowRejected(app, python);
    await assertSignedFollowReachesTypedCommunityInbox(app, privateKey, python);
    await assertSharedInboxRoutesByObject(app, privateKey, python);
    await assertRouteObjectMismatchRejected(app, privateKey, python);
    await assertUndoRouteObjectMismatchRejected(app, privateKey, python);
    await assertMismatchedActorDocumentRejected(app, privateKey, python);
    await assertUnsafeEndpointRejected(app, privateKey, python);
    await assertSignedUndoReachesTypedCommunityInbox(app, privateKey, python);
  });
  console.log("verify:personal feed control plane-public-community-follow-workflow passed");
}

async function withPublicWorkflowFixture(
  run: (context: PublicWorkflowFixtureContext) => Promise<void>,
): Promise<void> {
  const remoteKeys = await importFixedRsaKeyPair();
  const publicKeyPem = await exportPublicKeyPem(remoteKeys.publicKey);
  const privateKeyPem = await exportPrivateKeyPem(remoteKeys.privateKey);
  const python = startRecordingPythonFixture(publicKeyPem, privateKeyPem);
  const restoreFetch = installFixtureFetch(publicKeyPem, python);
  const app = createGatewayApp(buildConfig(python.origin));

  try {
    await run({ app, privateKey: remoteKeys.privateKey, python });
  } finally {
    restoreFetch();
    await python.close();
  }
}

async function assertUnsignedFollowRejected(app: ReturnType<typeof createGatewayApp>, python: PythonFixture): Promise<void> {
  const response = await fetchWithTimeout(app, new Request(`${ORIGIN}actors/communities/python/inbox`, {
    method: "POST",
    headers: { "Content-Type": "application/activity+json", Accept: "application/activity+json" },
    body: JSON.stringify(buildFollow("https://remote.example/activities/follow/unsigned", BOB_ACTOR, PYTHON_ACTOR)),
  }));
  assert.equal(response.status, 401, "unsigned community Follow must be rejected before Python delivery");
  assert.equal(python.events.length, 0);
}

async function assertSignedFollowReachesTypedCommunityInbox(
  app: ReturnType<typeof createGatewayApp>,
  privateKey: CryptoKey,
  python: PythonFixture,
): Promise<void> {
  const followId = "https://remote.example/activities/follow/python";
  const response = await fetchWithTimeout(app, await signedActivityRequest(
    buildFollow(followId, BOB_ACTOR, PYTHON_ACTOR, {
      inbox: "https://attacker.example/not-bobs-inbox",
      endpoints: { sharedInbox: "https://attacker.example/shared" },
    }),
    privateKey,
    `${BOB_ACTOR}#main-key`,
    "/actors/communities/python/inbox",
  ));
  assert.ok([202, 204].includes(response.status), `signed community Follow should be accepted, got ${response.status}`);

  const event = await waitForEvent(python, "local.follow_requested", 1);
  assert.equal(event.target_actor_kind, "community");
  assert.equal(event.target_actor_id, PYTHON_ACTOR);
  assert.equal(event.actor_id, BOB_ACTOR);
  assert.equal(event.object?.follow_activity_id, followId);
  assert.equal(event.object?.remote_inbox_url, BOB_INBOX);
  assert.equal(event.object?.remote_shared_inbox_url, BOB_SHARED_INBOX);
  assert.deepEqual(python.followers, [BOB_ACTOR]);

  const followers = await app.request("/actors/communities/python/followers", { headers: { Accept: "application/activity+json" } });
  assert.equal(followers.status, 200);
  const body = await followers.json() as Record<string, unknown>;
  assert.equal(body.totalItems, 1);
  assert.equal(body.first, `${ORIGIN}actors/communities/python/followers?page=true`);
}

async function assertSharedInboxRoutesByObject(
  app: ReturnType<typeof createGatewayApp>,
  privateKey: CryptoKey,
  python: PythonFixture,
): Promise<void> {
  const before = python.events.length;
  const followId = "https://remote.example/activities/follow/shared-python";
  const response = await fetchWithTimeout(app, await signedActivityRequest(
    buildFollow(followId, BOB_ACTOR, PYTHON_ACTOR),
    privateKey,
    `${BOB_ACTOR}#main-key`,
    "/inbox",
  ));
  assert.ok([202, 204].includes(response.status), `shared inbox community Follow should be accepted, got ${response.status}`);
  const event = await waitForEvent(python, "local.follow_requested", before + 1);
  assert.equal(event.target_actor_kind, "community");
  assert.equal(event.target_actor_id, PYTHON_ACTOR);
}

async function assertRouteObjectMismatchRejected(
  app: ReturnType<typeof createGatewayApp>,
  privateKey: CryptoKey,
  python: PythonFixture,
): Promise<void> {
  const before = python.events.length;
  const response = await fetchWithTimeout(app, await signedActivityRequest(
    buildFollow("https://remote.example/activities/follow/wrong-community", BOB_ACTOR, RUBY_ACTOR),
    privateKey,
    `${BOB_ACTOR}#main-key`,
    "/actors/communities/python/inbox",
  ));
  assert.ok([202, 204].includes(response.status), `mismatched community Follow should fail closed after auth, got ${response.status}`);
  await waitForNoAdditionalEvents(python, before, 250);
}

async function assertUndoRouteObjectMismatchRejected(
  app: ReturnType<typeof createGatewayApp>,
  privateKey: CryptoKey,
  python: PythonFixture,
): Promise<void> {
  const before = python.events.length;
  const undo = buildUndo(
    "https://remote.example/activities/undo/wrong-community",
    BOB_ACTOR,
    buildFollow("https://remote.example/activities/follow/ruby", BOB_ACTOR, RUBY_ACTOR),
  );
  const response = await fetchWithTimeout(app, await signedActivityRequest(undo, privateKey, `${BOB_ACTOR}#main-key`, "/actors/communities/python/inbox"));
  assert.ok([202, 204].includes(response.status), `mismatched community Undo should fail closed after auth, got ${response.status}`);
  await waitForNoAdditionalEvents(python, before, 250);
}

async function assertMismatchedActorDocumentRejected(
  app: ReturnType<typeof createGatewayApp>,
  privateKey: CryptoKey,
  python: PythonFixture,
): Promise<void> {
  const before = python.events.length;
  const response = await fetchWithTimeout(app, await signedActivityRequest(
    buildFollow("https://mismatch.example/activities/follow/python", MISMATCH_DOC_ACTOR, PYTHON_ACTOR),
    privateKey,
    `${MISMATCH_DOC_ACTOR}#main-key`,
    "/actors/communities/python/inbox",
  ));
  assert.ok([202, 204, 401].includes(response.status), `mismatched actor doc should fail closed, got ${response.status}`);
  await waitForNoAdditionalEvents(python, before, 250);
}

async function assertUnsafeEndpointRejected(
  app: ReturnType<typeof createGatewayApp>,
  privateKey: CryptoKey,
  python: PythonFixture,
): Promise<void> {
  const before = python.events.length;
  const response = await fetchWithTimeout(app, await signedActivityRequest(
    buildFollow("https://unsafe.example/activities/follow/private", UNSAFE_ACTOR, PYTHON_ACTOR),
    privateKey,
    `${UNSAFE_ACTOR}#main-key`,
    "/actors/communities/python/inbox",
  ));
  assert.ok([202, 204].includes(response.status), `unsafe actor endpoint should fail closed, got ${response.status}`);
  await waitForNoAdditionalEvents(python, before, 250);
}

async function assertSignedUndoReachesTypedCommunityInbox(
  app: ReturnType<typeof createGatewayApp>,
  privateKey: CryptoKey,
  python: PythonFixture,
): Promise<void> {
  const undo = buildUndo(
    "https://remote.example/activities/undo/python",
    BOB_ACTOR,
    buildFollow("https://remote.example/activities/follow/python", BOB_ACTOR, PYTHON_ACTOR),
  );
  const response = await fetchWithTimeout(app, await signedActivityRequest(undo, privateKey, `${BOB_ACTOR}#main-key`, "/actors/communities/python/inbox"));
  assert.ok([202, 204].includes(response.status), `signed community Undo should be accepted, got ${response.status}`);
  const event = await waitForEvent(python, "local.unfollow_requested", 1);
  assert.equal(event.target_actor_kind, "community");
  assert.equal(event.target_actor_id, PYTHON_ACTOR);
  assert.equal(event.actor_id, BOB_ACTOR);
  assert.deepEqual(python.followers, []);
}

function buildFollow(id: string, actor: string, object: string, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    "@context": "https://www.w3.org/ns/activitystreams",
    id,
    type: "Follow",
    actor,
    object,
    ...extra,
  };
}

function buildUndo(id: string, actor: string, object: Record<string, unknown>): Record<string, unknown> {
  return {
    "@context": "https://www.w3.org/ns/activitystreams",
    id,
    type: "Undo",
    actor,
    object,
  };
}

async function signedActivityRequest(activity: Record<string, unknown>, privateKey: CryptoKey, keyId: string, path: string): Promise<Request> {
  const body = JSON.stringify(activity);
  const bodyBytes = new TextEncoder().encode(body);
  return await signRequest(new Request(`${ORIGIN.replace(/\/$/, "")}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/activity+json", Accept: "application/activity+json" },
    body,
  }), privateKey, new URL(keyId), {
    body: bodyBytes.buffer.slice(bodyBytes.byteOffset, bodyBytes.byteOffset + bodyBytes.byteLength),
  });
}

function installFixtureFetch(publicKeyPem: string, python: PythonFixture): () => void {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const url = input instanceof URL ? input.href : typeof input === "string" ? input : input.url;
    const pythonResponse = await python.fetch(url, init);
    if (pythonResponse != null) return pythonResponse;
    if (url in preloadedContexts) {
      return new Response(JSON.stringify(preloadedContexts[url]), { status: 200, headers: { "Content-Type": "application/ld+json" } });
    }
    if (url === PYTHON_ACTOR || url === `${PYTHON_ACTOR}#main-key`) {
      return activityJsonResponse(buildActorDocument(PYTHON_ACTOR, `${PYTHON_ACTOR}/inbox`, publicKeyPem, BOB_SHARED_INBOX, "Group"));
    }
    if (url === RUBY_ACTOR || url === `${RUBY_ACTOR}#main-key`) {
      return activityJsonResponse(buildActorDocument(RUBY_ACTOR, `${RUBY_ACTOR}/inbox`, publicKeyPem, BOB_SHARED_INBOX, "Group"));
    }
    if (url === BOB_ACTOR || url === `${BOB_ACTOR}#main-key`) {
      return activityJsonResponse(buildActorDocument(BOB_ACTOR, BOB_INBOX, publicKeyPem, BOB_SHARED_INBOX, "Person"));
    }
    if (url === MISMATCH_DOC_ACTOR || url === `${MISMATCH_DOC_ACTOR}#main-key`) {
      return activityJsonResponse(buildActorDocument(OTHER_ACTOR, "https://mismatch.example/u/bob/inbox", publicKeyPem, null, "Person", MISMATCH_DOC_ACTOR));
    }
    if (url === OTHER_ACTOR || url === `${OTHER_ACTOR}#main-key`) {
      return activityJsonResponse(buildActorDocument(OTHER_ACTOR, "https://other.example/u/mallory/inbox", publicKeyPem, null, "Person"));
    }
    if (url === UNSAFE_ACTOR || url === `${UNSAFE_ACTOR}#main-key`) {
      return activityJsonResponse(buildActorDocument(UNSAFE_ACTOR, "https://[::ffff:127.0.0.1]/community-follow", publicKeyPem, null, "Person"));
    }
    return new Response("fixture remote document not found", { status: 404 });
  };
  return () => { globalThis.fetch = originalFetch; };
}

function buildActorDocument(
  id: string,
  inbox: string,
  publicKeyPem: string,
  sharedInbox: string | null,
  type: "Person" | "Group",
  keyOwner: string = id,
): Record<string, unknown> {
  const document: Record<string, unknown> = {
    "@context": ["https://www.w3.org/ns/activitystreams", "https://w3id.org/security/v1"],
    id,
    type,
    preferredUsername: id.endsWith("python") ? "python" : "bob",
    inbox,
    publicKey: { id: `${keyOwner}#main-key`, owner: keyOwner, publicKeyPem },
  };
  if (sharedInbox != null) document.endpoints = { sharedInbox };
  return document;
}

function activityJsonResponse(document: Record<string, unknown>): Response {
  return new Response(JSON.stringify(document), { status: 200, headers: { "Content-Type": "application/activity+json" } });
}

function startRecordingPythonFixture(publicKeyPem: string, privateKeyPem: string): PythonFixture {
  const events: CapturedBridgeEvent[] = [];
  const followers: string[] = [];
  pythonFixtureCounter += 1;
  const origin = `http://python-community-${pythonFixtureCounter}.fixture`;
  return {
    origin,
    events,
    followers,
    async fetch(url: string, init?: RequestInit): Promise<Response | null> {
      if (!url.startsWith(origin)) return null;
      const requestUrl = new URL(url);
      const method = init?.method ?? "GET";
      const body = typeof init?.body === "string" ? init.body : "";

      if (requestUrl.pathname === "/internal/activitypub/events" && method === "POST") {
        const event = JSON.parse(body) as CapturedBridgeEvent;
        events.push(event);
        if (event.event_type === "local.follow_requested" && event.actor_id != null && !followers.includes(event.actor_id)) {
          followers.push(event.actor_id);
        }
        if (event.event_type === "local.unfollow_requested" && event.actor_id != null) {
          const index = followers.indexOf(event.actor_id);
          if (index >= 0) followers.splice(index, 1);
        }
        return jsonResponse({ status: "processed", outcome: "delivered", detail: "fixture" });
      }

      if (requestUrl.pathname === "/internal/fedify/actors/communities/python") {
        return jsonResponse(communityRow("python", publicKeyPem, privateKeyPem));
      }
      if (requestUrl.pathname === "/internal/fedify/actors/communities/ruby") {
        return jsonResponse(communityRow("ruby", publicKeyPem, privateKeyPem));
      }
      if (requestUrl.pathname === "/internal/fedify/actors/communities/resolve" && method === "POST") {
        const actorUrl = (JSON.parse(body) as Record<string, unknown>).actor_url;
        if (actorUrl === PYTHON_ACTOR) return jsonResponse(communityRow("python", publicKeyPem, privateKeyPem));
        if (actorUrl === RUBY_ACTOR) return jsonResponse(communityRow("ruby", publicKeyPem, privateKeyPem));
        return jsonResponse({ detail: "not found" }, 404);
      }
      if (requestUrl.pathname === "/internal/fedify/actors/communities/python/followers") {
        const page = Number(requestUrl.searchParams.get("page") ?? "1");
        const pageSize = Number(requestUrl.searchParams.get("page_size") ?? "40");
        const start = (page - 1) * pageSize;
        return jsonResponse({
          followers_url: `${PYTHON_ACTOR}/followers`,
          total_items: followers.length,
          page,
          page_size: pageSize,
          actor_ids: followers.slice(start, start + pageSize),
        });
      }

      return jsonResponse({ detail: "not found" }, 404);
    },
    async close() {},
  };
}

function communityRow(slug: "python" | "ruby", publicKeyPem: string, privateKeyPem: string): Record<string, unknown> {
  const actor = slug === "python" ? PYTHON_ACTOR : RUBY_ACTOR;
  return {
    slug,
    actor_url: actor,
    inbox_url: `${actor}/inbox`,
    outbox_url: `${actor}/outbox`,
    followers_url: `${actor}/followers`,
    display_name: slug,
    summary: `${slug} summary`,
    public_key_pem: publicKeyPem,
    private_key_pem: privateKeyPem,
  };
}

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
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
    skipSignatureVerificationForTests: true,
  };
}

async function fetchWithTimeout(app: ReturnType<typeof createGatewayApp>, request: Request): Promise<Response> {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`public community inbox request timed out for ${request.method} ${new URL(request.url).pathname}`)), 12_000);
  });
  try {
    return await Promise.race([app.fetch(request), timeout]);
  } finally {
    if (timer != null) clearTimeout(timer);
  }
}

async function waitForNoAdditionalEvents(python: PythonFixture, expectedCount: number, durationMs: number): Promise<void> {
  const deadline = Date.now() + durationMs;
  while (Date.now() < deadline) {
    assert.equal(python.events.length, expectedCount, "unexpected extra Python event while waiting for a stable community workflow state");
    await wait(25);
  }
}

async function waitForEvent(python: PythonFixture, eventType: string, count: number): Promise<CapturedBridgeEvent> {
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

void main().then(
  () => process.exit(0),
  (error) => {
    console.error(error);
    process.exit(1);
  },
);
