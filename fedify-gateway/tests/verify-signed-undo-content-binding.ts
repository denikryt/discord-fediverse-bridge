/** Exercise signed Undo(Follow) HTTP ingress across Fedify's queued listener. */

import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";

import { signRequest } from "@fedify/fedify";
import { preloadedContexts } from "@fedify/vocab-runtime";

import type { GatewayConfig } from "../src/config.js";
import { createGatewayFederation } from "../src/federation.js";
import { createGatewayApp } from "../src/server.js";
import { fixtureAuthenticatedDocumentLoaderFactory, fixtureDocumentLoaderFactory } from "./support/fixture-document-loader.js";
import { importFixedRsaKeyPair } from "./support/python-bridge-fixture.js";

const ORIGIN = "https://bridge.example/";
const ACTOR = "https://remote.example/u/bob";
const UNDO_ID = "https://remote.example/activities/undo/1";
const TARGET_A = `${ORIGIN}actors/communities/alpha`;
const TARGET_B = `${ORIGIN}actors/communities/beta`;

/** A rejected same-ID request must not change the accepted Undo's target. */
async function main(): Promise<void> {
  const keys = await importFixedRsaKeyPair();
  const publicKeyPem = await exportPublicKeyPem(keys.publicKey);
  const events: Array<Record<string, unknown>> = [];
  const restoreFetch = installFixtureFetch(publicKeyPem, events);
  const config: GatewayConfig = {
    actorIdentifier: "bridge", actorName: "Bridge", actorSummary: "Bridge",
    fedifyOrigin: ORIGIN, port: 3000,
    pythonBridgeInternalUrl: "http://python-undo.fixture",
    pythonBridgeSharedSecret: "secret", logLevel: "info",
    documentLoaderFactoryForTests: fixtureDocumentLoaderFactory,
    authenticatedDocumentLoaderFactoryForTests: fixtureAuthenticatedDocumentLoaderFactory,
    manuallyStartQueueForTests: true,
  };
  const fedify = createGatewayFederation(config);
  const app = createGatewayApp(config, fedify);
  const queueStop = new AbortController();
  let worker: Promise<void> | null = null;
  try {
    const accepted = await app.fetch(await signedUndoRequest(makeUndo(TARGET_A), keys.privateKey));
    assert.equal(accepted.status, 202, "valid HTTP signature must enqueue Undo");

    // Capture of the later request happens before Fedify rejects its invalid
    // signature; the queued accepted listener must still use its own payload.
    const rejected = await app.fetch(new Request(`${ORIGIN}inbox`, {
      method: "POST",
      headers: {
        "Content-Type": "application/activity+json",
        Accept: "application/activity+json",
        Signature: "invalid",
      },
      body: JSON.stringify(makeUndo(TARGET_B)),
    }));
    assert.equal(rejected.status, 401);

    worker = fedify.startQueue(config, { signal: queueStop.signal, queue: "inbox" });
    await waitForEvent(events);
    assert.equal(events.length, 1);
    assert.equal(events[0]?.event_type, "local.unfollow_requested");
    assert.equal(events[0]?.target_actor_id, TARGET_A);
    assert.equal((events[0]?.object as Record<string, unknown>).follow_activity_id,
      "https://remote.example/activities/follow/alpha");
  } finally {
    queueStop.abort();
    await worker?.catch(() => {});
    restoreFetch();
  }
  console.log("verify:signed-undo-content-binding passed");
}

/** Build distinct embedded Follows under the same outer Undo ID. */
function makeUndo(target: string): Record<string, unknown> {
  const slug = target === TARGET_A ? "alpha" : "beta";
  return {
    "@context": "https://www.w3.org/ns/activitystreams",
    type: "Undo", id: UNDO_ID, actor: ACTOR,
    object: {
      type: "Follow", id: `https://remote.example/activities/follow/${slug}`,
      actor: ACTOR, object: target,
    },
  };
}

/** Sign the actual HTTP body with the remote actor's test key. */
async function signedUndoRequest(activity: Record<string, unknown>, privateKey: CryptoKey): Promise<Request> {
  const body = JSON.stringify(activity);
  const bytes = new TextEncoder().encode(body);
  return await signRequest(new Request(`${ORIGIN}inbox`, {
    method: "POST",
    headers: { "Content-Type": "application/activity+json", Accept: "application/activity+json" },
    body,
  }), privateKey, new URL(`${ACTOR}#main-key`), {
    body: bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
  });
}

/** Serve only synthetic actors, communities, and the observable Python boundary. */
function installFixtureFetch(publicKeyPem: string, events: Array<Record<string, unknown>>): () => void {
  const original = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const url = input instanceof URL ? input.href : typeof input === "string" ? input : input.url;
    if (url.startsWith("http://python-undo.fixture/")) {
      if (url.endsWith("/internal/activitypub/events")) {
        events.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
        return jsonResponse({ status: "processed", outcome: "delivered" });
      }
      const slug = url.match(/\/internal\/fedify\/actors\/communities\/(alpha|beta)$/)?.[1];
      if (slug != null) return jsonResponse(communityRow(slug));
      return jsonResponse({ detail: "not found" }, 404);
    }
    if (url in preloadedContexts) return jsonResponse(preloadedContexts[url]);
    if (url === ACTOR || url === `${ACTOR}#main-key`) {
      return jsonResponse({
        "@context": ["https://www.w3.org/ns/activitystreams", "https://w3id.org/security/v1"],
        type: "Person", id: ACTOR, inbox: `${ACTOR}/inbox`,
        publicKey: { id: `${ACTOR}#main-key`, owner: ACTOR, publicKeyPem },
      });
    }
    return jsonResponse({ detail: "not found" }, 404);
  };
  return () => { globalThis.fetch = original; };
}

/** Give the bridge a persisted local community target without a real database. */
function communityRow(slug: string): Record<string, unknown> {
  const actor = `${ORIGIN}actors/communities/${slug}`;
  return {
    slug, actor_url: actor, inbox_url: `${actor}/inbox`,
    outbox_url: `${actor}/outbox`, followers_url: `${actor}/followers`,
    display_name: slug, summary: "", public_key_pem: "public", private_key_pem: "private",
  };
}

/** Return a JSON-LD fixture response with no network access. */
function jsonResponse(document: unknown, status = 200): Response {
  return new Response(JSON.stringify(document), {
    status, headers: { "Content-Type": "application/activity+json" },
  });
}

/** Export the public half of the fixed signing key as PEM. */
async function exportPublicKeyPem(key: CryptoKey): Promise<string> {
  const bytes = Buffer.from(await webcrypto.subtle.exportKey("spki", key));
  const lines = bytes.toString("base64").match(/.{1,64}/g)?.join("\n") ?? "";
  return `-----BEGIN PUBLIC KEY-----\n${lines}\n-----END PUBLIC KEY-----\n`;
}

/** Wait for a real event delivery, failing rather than accepting HTTP 202 alone. */
async function waitForEvent(events: Array<Record<string, unknown>>): Promise<void> {
  const deadline = Date.now() + 6000;
  while (events.length === 0 && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.ok(events.length > 0, "signed Undo must reach the Python boundary");
}

await main();
