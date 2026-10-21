/** Exercise signed Announce HTTP ingress across Fedify's queued listener. */

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
const RELAY = "https://relay.example/c/python";
const AUTHOR = "https://author.example/u/bob";
const CREATE_ID = "https://author.example/activities/create/1";

/** Signed delivery must use its own body after another request reuses its ID. */
async function main(): Promise<void> {
  const keys = await importFixedRsaKeyPair();
  const publicKeyPem = await exportPublicKeyPem(keys.publicKey);
  const originalCreate = makeCreate("original signed body");
  const replacementCreate = makeCreate("replacement body");
  const events: Array<Record<string, unknown>> = [];
  const restoreFetch = installFixtureFetch(publicKeyPem, originalCreate, events);
  const config: GatewayConfig = {
    actorIdentifier: "bridge", actorName: "Bridge", actorSummary: "Bridge",
    fedifyOrigin: ORIGIN, port: 3000,
    pythonBridgeInternalUrl: "http://python-announcement.fixture",
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
    const signed = await signedAnnounceRequest(makeAnnounce(originalCreate), keys.privateKey);
    const accepted = await app.fetch(signed);
    assert.equal(accepted.status, 202, "valid HTTP signature must enqueue Announce");

    // Raw capture precedes authentication. A later invalid request can reuse
    // both IDs while the accepted job is queued, but must not alter that job.
    const forged = new Request(`${ORIGIN}inbox`, {
      method: "POST",
      // A forged signature header passes the Hono prefilter; Fedify must still
      // reject it after the raw body has reached the shared capture cache.
      headers: {
        "Content-Type": "application/activity+json",
        Accept: "application/activity+json",
        Signature: "invalid",
      },
      body: JSON.stringify(makeAnnounce(replacementCreate)),
    });
    const rejected = await app.fetch(forged);
    assert.equal(rejected.status, 401);

    worker = fedify.startQueue(config, { signal: queueStop.signal, queue: "inbox" });
    await waitForEvent(events);
    assert.equal(events.length, 1);
    assert.equal((events[0]?.object as Record<string, unknown>).body_markdown, "original signed body");
  } finally {
    queueStop.abort();
    await worker?.catch(() => {});
    restoreFetch();
  }
  console.log("verify:signed-announce-content-binding passed");
}

/** Build an author-origin post accepted by the normal Create(Page) path. */
function makeCreate(content: string): Record<string, unknown> {
  return {
    type: "Create", id: CREATE_ID, actor: AUTHOR,
    object: {
      type: "Page", id: "https://author.example/post/1", attributedTo: AUTHOR,
      name: "A post", content,
      audience: "https://relay.example/c/python",
    },
  };
}

/** Wrap a nested Create in the relayer's stable Announce ID. */
function makeAnnounce(create: Record<string, unknown>): Record<string, unknown> {
  return {
    "@context": "https://www.w3.org/ns/activitystreams",
    type: "Announce", id: "https://relay.example/announces/1", actor: RELAY,
    object: create,
  };
}

/** Sign the actual HTTP body with the relayer's test key. */
async function signedAnnounceRequest(activity: Record<string, unknown>, privateKey: CryptoKey): Promise<Request> {
  const body = JSON.stringify(activity);
  const bytes = new TextEncoder().encode(body);
  return await signRequest(new Request(`${ORIGIN}inbox`, {
    method: "POST",
    headers: { "Content-Type": "application/activity+json", Accept: "application/activity+json" },
    body,
  }), privateKey, new URL(`${RELAY}#main-key`), {
    body: bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
  });
}

/** Serve only fixture documents and capture the real Python event HTTP call. */
function installFixtureFetch(
  publicKeyPem: string,
  create: Record<string, unknown>,
  events: Array<Record<string, unknown>>,
): () => void {
  const original = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const url = input instanceof URL ? input.href : typeof input === "string" ? input : input.url;
    if (url.startsWith("http://python-announcement.fixture/")) {
      if (url.endsWith("/internal/activitypub/events")) {
        events.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
        return jsonResponse({ status: "processed", outcome: "delivered" });
      }
      return jsonResponse({ detail: "not found" }, 404);
    }
    if (url in preloadedContexts) return jsonResponse(preloadedContexts[url]);
    if (url === RELAY || url === `${RELAY}#main-key`) {
      return jsonResponse({
        "@context": ["https://www.w3.org/ns/activitystreams", "https://w3id.org/security/v1"],
        type: "Group", id: RELAY, inbox: `${RELAY}/inbox`,
        publicKey: { id: `${RELAY}#main-key`, owner: RELAY, publicKeyPem },
      });
    }
    if (url === CREATE_ID) return jsonResponse({ "@context": "https://www.w3.org/ns/activitystreams", ...create });
    return jsonResponse({ detail: "not found" }, 404);
  };
  return () => { globalThis.fetch = original; };
}

/** Return a JSON-LD fixture response with no network access. */
function jsonResponse(document: unknown, status = 200): Response {
  return new Response(JSON.stringify(document), {
    status, headers: { "Content-Type": "application/activity+json" },
  });
}

/** Export the public half of the fixed test signing key as PEM. */
async function exportPublicKeyPem(key: CryptoKey): Promise<string> {
  const bytes = Buffer.from(await webcrypto.subtle.exportKey("spki", key));
  const lines = bytes.toString("base64").match(/.{1,64}/g)?.join("\n") ?? "";
  return `-----BEGIN PUBLIC KEY-----\n${lines}\n-----END PUBLIC KEY-----\n`;
}

/** Await the observable bridge event after Fedify starts its queue worker. */
async function waitForEvent(events: Array<Record<string, unknown>>): Promise<void> {
  const deadline = Date.now() + 6000;
  while (events.length === 0 && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.ok(events.length > 0, "signed Announce must reach the Python boundary");
}

await main();
