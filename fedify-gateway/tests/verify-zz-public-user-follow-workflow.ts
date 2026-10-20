/**
 * Public User Actor Follow workflow coverage for federation ingress.
 *
 * These tests drive the production Hono/Fedify app through
 * POST /actors/users/alice/inbox. Remote actor dereference happens through the
 * normal fetch/document-loader boundary, Python delivery through the internal
 * HTTP bridge boundary, and followers visibility through the public collection
 * route. The test intentionally does not call buildLocalFollowRequestedEvent()
 * or buildLocalUnfollowRequestedEvent() directly.
 */
import assert from "node:assert/strict";
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
const MISMATCH_DOC_ACTOR = "https://mismatch.example/users/bob";
const BOB_IMPERSONATOR_ACTOR = `${BOB_ACTOR}/impersonator`;
const UNSAFE_ACTOR = "https://unsafe.example/users/bob";
const OTHER_ACTOR = "https://other.example/users/mallory";
let pythonFixtureCounter = 0;

const SCENARIOS = [
  "unsigned-follow",
  "signed-follow",
  "duplicate-follow",
  "mismatched-follow-actor",
  "unsafe-follow-endpoint",
  "undo-workflow",
] as const;

type ScenarioName = typeof SCENARIOS[number];

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
  const scenarioArgument = process.argv.find((argument) => argument.startsWith("--scenario="));
  if (scenarioArgument != null) {
    await runScenario(parseScenarioName(scenarioArgument.slice("--scenario=".length)));
    return;
  }

  for (const scenario of SCENARIOS) {
    await runScenario(scenario);
  }

  console.log("verify:user-actor-public-follow-workflow passed");
}

async function runScenario(scenario: ScenarioName): Promise<void> {
  switch (scenario) {
    case "unsigned-follow":
      await withPublicWorkflowFixture(async ({ app, python }) => {
        await assertUnsignedFollowRejected(app, python);
      });
      return;
    case "signed-follow":
      await withPublicWorkflowFixture(async ({ app, privateKey, python }) => {
        await assertSignedFollowReachesPublicListener(app, privateKey, python, "https://remote.example/activities/follow/happy");
      });
      return;
    case "duplicate-follow":
      await withPublicWorkflowFixture(async ({ app, privateKey, python }) => {
        const followId = "https://remote.example/activities/follow/duplicate";
        await assertSignedFollowReachesPublicListener(app, privateKey, python, followId);
        await assertSameIdPublicDuplicateDoesNotReachPythonAgain(app, privateKey, python, followId);
      });
      return;
    case "mismatched-follow-actor":
      await withPublicWorkflowFixture(async ({ app, privateKey, python }) => {
        await assertMismatchedActorDocumentRejected(app, privateKey, python);
      });
      return;
    case "unsafe-follow-endpoint":
      await withPublicWorkflowFixture(async ({ app, privateKey, python }) => {
        await assertUnsafeEndpointRejected(app, privateKey, python);
      });
      return;
    case "undo-workflow":
      await withPublicWorkflowFixture(async ({ app, privateKey, python }) => {
        const followId = "https://remote.example/activities/follow/undo";
        await assertSignedFollowReachesPublicListener(app, privateKey, python, followId);
        await assertUnsignedPublicUndoRejectedWithoutPythonEvent(app, python, followId);
        await assertMismatchedSignedUndoRejectedWithoutPythonEvent(app, privateKey, python);
        await assertSignedUndoReachesPublicListener(app, privateKey, python, followId);
      });
      return;
  }
}

function parseScenarioName(value: string): ScenarioName {
  if ((SCENARIOS as readonly string[]).includes(value)) return value as ScenarioName;
  throw new Error(`unknown public workflow scenario: ${value}`);
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
  const response = await fetchWithTimeout(app, new Request(`${ORIGIN}actors/users/alice/inbox`, {
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
  followId: string,
): Promise<void> {
  const follow = buildFollow(followId, BOB_ACTOR, {
    inbox: "https://attacker.example/not-bobs-inbox",
    endpoints: { sharedInbox: "https://attacker.example/shared" },
  });
  const response = await fetchWithTimeout(app, await signedActivityRequest(follow, privateKey, `${BOB_ACTOR}#main-key`));
  assert.ok([202, 204].includes(response.status), `signed public Follow should be accepted, got ${response.status}`);

  const event = await waitForEvent(python, "local.follow_requested", 1);
  assert.equal(event.target_actor_kind, "user");
  assert.equal(event.target_actor_id, ALICE_ACTOR);
  assert.equal(event.actor_id, BOB_ACTOR);
  assert.equal(event.object?.follow_activity_id, followId);
  assert.equal(event.object?.remote_inbox_url, BOB_INBOX);
  assert.equal(event.object?.remote_shared_inbox_url, BOB_SHARED_INBOX);
  assert.deepEqual(python.followers, [BOB_ACTOR]);

  await waitForNoAdditionalEvents(python, python.events.length, 250);

  const followers = await app.request("/actors/users/alice/followers", { headers: { Accept: "application/activity+json" } });
  assert.equal(followers.status, 200);
  const body = await followers.json() as Record<string, unknown>;
  assert.equal(body.totalItems, 1);
  assert.equal(body.first, `${ORIGIN}actors/users/alice/followers?page=true`);
}

async function assertSameIdPublicDuplicateDoesNotReachPythonAgain(
  app: ReturnType<typeof createGatewayApp>,
  privateKey: CryptoKey,
  python: PythonFixture,
  followId: string,
): Promise<void> {
  const before = python.events.length;
  const response = await fetchWithTimeout(app, await signedActivityRequest(buildFollow(followId, BOB_ACTOR), privateKey, `${BOB_ACTOR}#main-key`));
  assert.ok([202, 204].includes(response.status), `duplicate Follow should be accepted/idempotent, got ${response.status}`);
  await waitForNoAdditionalEvents(python, before, 250);
  assert.equal(python.events.length, before, "Fedify per-inbox idempotency means same-ID public duplicate is not an Accept recovery path");
}

async function assertMismatchedActorDocumentRejected(
  app: ReturnType<typeof createGatewayApp>,
  privateKey: CryptoKey,
  python: PythonFixture,
): Promise<void> {
  const before = python.events.length;
  const follow = buildFollow("https://remote.example/activities/follow/mismatch", MISMATCH_DOC_ACTOR);
  const response = await fetchWithTimeout(app, await signedActivityRequest(follow, privateKey, `${MISMATCH_DOC_ACTOR}#main-key`));
  assert.ok([202, 204, 401].includes(response.status), `mismatched actor doc should fail closed at public boundary, got ${response.status}`);
  await waitForNoAdditionalEvents(python, before, 250);
  assert.equal(before, python.events.length, "mismatched actor-document id must not emit a Python event");
}

async function assertUnsafeEndpointRejected(
  app: ReturnType<typeof createGatewayApp>,
  privateKey: CryptoKey,
  python: PythonFixture,
): Promise<void> {
  const before = python.events.length;
  const follow = buildFollow("https://unsafe.example/activities/follow/private", UNSAFE_ACTOR);
  const response = await fetchWithTimeout(app, await signedActivityRequest(follow, privateKey, `${UNSAFE_ACTOR}#main-key`));
  assert.ok([202, 204].includes(response.status), `unsafe endpoint should fail closed in listener, got ${response.status}`);
  await waitForNoAdditionalEvents(python, before, 250);
  assert.equal(before, python.events.length, "unsafe dereferenced inbox must not emit a Python event");
}

async function assertUnsignedPublicUndoRejectedWithoutPythonEvent(
  app: ReturnType<typeof createGatewayApp>,
  python: PythonFixture,
  followId: string,
): Promise<void> {
  const before = python.events.length;
  const undo = buildUndo("https://remote.example/activities/undo/unsigned", BOB_ACTOR, buildFollow(followId, BOB_ACTOR));
  const response = await fetchWithTimeout(app, new Request(`${ORIGIN}actors/users/alice/inbox`, {
    method: "POST",
    headers: { "Content-Type": "application/activity+json", Accept: "application/activity+json" },
    body: JSON.stringify(undo),
  }));
  assert.equal(response.status, 401, "unsigned public Undo must reach Fedify auth through the typed inbox, not route-level 404");
  assert.equal(python.events.length, before, "unsigned public Undo must not emit a Python event");

  const followers = await app.request("/actors/users/alice/followers", { headers: { Accept: "application/activity+json" } });
  assert.equal(followers.status, 200);
  const body = await followers.json() as Record<string, unknown>;
  assert.equal(body.totalItems, 1);
}

async function assertMismatchedSignedUndoRejectedWithoutPythonEvent(
  app: ReturnType<typeof createGatewayApp>,
  privateKey: CryptoKey,
  python: PythonFixture,
): Promise<void> {
  const before = python.events.length;
  const undo = buildUndo(
    "https://remote.example/activities/undo/mismatch",
    BOB_IMPERSONATOR_ACTOR,
    buildFollow("https://remote.example/activities/follow/impersonated", BOB_ACTOR),
  );
  const response = await fetchWithTimeout(app, await signedActivityRequest(undo, privateKey, `${BOB_IMPERSONATOR_ACTOR}#main-key`));
  assert.ok([202, 204, 401].includes(response.status), `mismatched public Undo should fail closed at public boundary, got ${response.status}`);
  await waitForNoAdditionalEvents(python, before, 250);
  assert.equal(python.events.length, before, "Undo actor must match embedded Follow actor before emitting a Python event");
  assert.deepEqual(python.followers, [BOB_ACTOR]);
}

async function assertSignedUndoReachesPublicListener(
  app: ReturnType<typeof createGatewayApp>,
  privateKey: CryptoKey,
  python: PythonFixture,
  followId: string,
): Promise<void> {
  const undo = buildUndo("https://remote.example/activities/undo/1", BOB_ACTOR, buildFollow(followId, BOB_ACTOR));
  const response = await fetchWithTimeout(app, await signedActivityRequest(undo, privateKey, `${BOB_ACTOR}#main-key`));
  assert.ok([202, 204].includes(response.status), `signed public Undo should be accepted, got ${response.status}`);

  const event = await waitForEvent(python, "local.unfollow_requested", 1);
  assert.equal(event.target_actor_kind, "user");
  assert.equal(event.target_actor_id, ALICE_ACTOR);
  assert.equal(event.actor_id, BOB_ACTOR);
  assert.equal(event.object?.follow_activity_id, followId);
  assert.deepEqual(python.followers, [], "signed public Undo must remove Bob from the fixture relationship state");

  const followers = await app.request("/actors/users/alice/followers", { headers: { Accept: "application/activity+json" } });
  assert.equal(followers.status, 200);
  const body = await followers.json() as Record<string, unknown>;
  assert.equal(body.totalItems, 0);
}

function buildFollow(id: string, actor: string, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    "@context": "https://www.w3.org/ns/activitystreams",
    id,
    type: "Follow",
    actor,
    object: ALICE_ACTOR,
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

async function signedActivityRequest(activity: Record<string, unknown>, privateKey: CryptoKey, keyId: string): Promise<Request> {
  const body = JSON.stringify(activity);
  const bodyBytes = new TextEncoder().encode(body);
  return await signRequest(new Request(`${ORIGIN}actors/users/alice/inbox`, {
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
    if (url === ALICE_ACTOR) {
      return activityJsonResponse(buildActorDocument(ALICE_ACTOR, `${ALICE_ACTOR}/inbox`, publicKeyPem, null));
    }
    if (url.startsWith("https://remote.example/activities/follow/")) {
      return activityJsonResponse(buildFollow(url, url.endsWith("/impersonated") ? BOB_ACTOR : BOB_ACTOR));
    }
    if (url === BOB_ACTOR || url === `${BOB_ACTOR}#main-key`) {
      return activityJsonResponse(buildActorDocument(BOB_ACTOR, BOB_INBOX, publicKeyPem, BOB_SHARED_INBOX));
    }
    if (url === BOB_IMPERSONATOR_ACTOR || url === `${BOB_IMPERSONATOR_ACTOR}#main-key`) {
      return activityJsonResponse(buildActorDocument(BOB_IMPERSONATOR_ACTOR, `${BOB_IMPERSONATOR_ACTOR}/inbox`, publicKeyPem, null));
    }
    if (url === MISMATCH_DOC_ACTOR || url === `${MISMATCH_DOC_ACTOR}#main-key`) {
      return activityJsonResponse(buildActorDocument(OTHER_ACTOR, "https://mismatch.example/users/bob/inbox", publicKeyPem, null, MISMATCH_DOC_ACTOR));
    }
    if (url === OTHER_ACTOR || url === `${OTHER_ACTOR}#main-key`) {
      return activityJsonResponse(buildActorDocument(OTHER_ACTOR, "https://other.example/users/mallory/inbox", publicKeyPem, null));
    }
    if (url === UNSAFE_ACTOR || url === `${UNSAFE_ACTOR}#main-key`) {
      return activityJsonResponse(buildActorDocument(UNSAFE_ACTOR, "https://[::ffff:127.0.0.1]/accept-user-actor-follow", publicKeyPem, null));
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
  keyOwner: string = id,
): Record<string, unknown> {
  const document: Record<string, unknown> = {
    "@context": ["https://www.w3.org/ns/activitystreams", "https://w3id.org/security/v1"],
    id,
    type: "Person",
    preferredUsername: "bob",
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
  const origin = `http://python-${pythonFixtureCounter}.fixture`;
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

      if (requestUrl.pathname === "/internal/fedify/actors/users/alice") {
        return jsonResponse({
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

      if (requestUrl.pathname === "/internal/fedify/actors/users/alice/followers") {
        const page = Number(requestUrl.searchParams.get("page") ?? "1");
        const pageSize = Number(requestUrl.searchParams.get("page_size") ?? "40");
        const start = (page - 1) * pageSize;
        return jsonResponse({
          followers_url: `${ALICE_ACTOR}/followers`,
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
    timer = setTimeout(() => reject(new Error(`public inbox request timed out for ${request.method} ${new URL(request.url).pathname}`)), 12_000);
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
    assert.equal(python.events.length, expectedCount, "unexpected extra Python event while waiting for a stable public workflow state");
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
