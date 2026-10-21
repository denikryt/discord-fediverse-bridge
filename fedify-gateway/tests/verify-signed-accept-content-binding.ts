/** Exercise signed Accept(Follow) ingress across Fedify's queued listener. */

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
const COMMUNITY = "https://remote.example/c/alpha";
const FOLLOW_A = `${ORIGIN}activities/follow/alpha`;
const FOLLOW_B = `${ORIGIN}activities/follow/beta`;

/** Valid references survive cache collision; malformed Accepts cause no event. */
async function main(): Promise<void> {
  await runScenario("valid", FOLLOW_A, "Follow", FOLLOW_A);
  await runScenario("url-reference", FOLLOW_A, "Follow", FOLLOW_A, { asUrl: true });
  await runScenario("missing-id", null, "Follow", null);
  await runScenario("wrong-type", FOLLOW_A, "Like", null);
  await runScenario("wrong-follow-actor", FOLLOW_A, "Follow", null, {
    followActor: "https://unrelated.example/u/mallory",
  });
  await runScenario("wrong-follow-target", FOLLOW_A, "Follow", null, {
    followTarget: "https://remote.example/c/other",
  });
  console.log("verify:signed-accept-content-binding passed");
}

/** Exercise one accepted signed body followed by an invalid same-ID body. */
async function runScenario(
  name: string,
  signedFollowId: string | null,
  signedObjectType: string,
  expectedFollowId: string | null,
  options: { asUrl?: boolean; followActor?: string; followTarget?: string } = {},
): Promise<void> {
  const keys = await importFixedRsaKeyPair();
  const publicKeyPem = await exportPublicKeyPem(keys.publicKey);
  const events: Array<Record<string, unknown>> = [];
  const restoreFetch = installFixtureFetch(publicKeyPem, events);
  const acceptId = `https://remote.example/activities/accept/${name}`;
  const config: GatewayConfig = {
    actorIdentifier: "bridge", actorName: "Bridge", actorSummary: "Bridge",
    fedifyOrigin: ORIGIN, port: 3000,
    pythonBridgeInternalUrl: "http://python-accept.fixture",
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
    const accepted = await app.fetch(await signedAcceptRequest(
      makeAccept(signedFollowId, acceptId, signedObjectType, options), keys.privateKey,
    ));
    assert.equal(accepted.status, 202, "valid signature must enqueue Accept");

    // Hono captures this body before Fedify rejects its forged signature.
    const rejected = await app.fetch(new Request(`${ORIGIN}inbox`, {
      method: "POST",
      headers: {
        "Content-Type": "application/activity+json",
        Accept: "application/activity+json",
        Signature: "invalid",
      },
      body: JSON.stringify(makeAccept(FOLLOW_B, acceptId, "Follow")),
    }));
    assert.equal(rejected.status, 401);

    const controlId = `${acceptId}/control`;
    if (expectedFollowId == null) {
      // A later valid control gives the FIFO queue a concrete completion
      // marker. Any event for the malformed Accept appears before it.
      const control = await app.fetch(await signedAcceptRequest(
        makeAccept(FOLLOW_A, controlId, "Follow"), keys.privateKey,
      ));
      assert.equal(control.status, 202);
    }

    worker = fedify.startQueue(config, { signal: queueStop.signal, queue: "inbox" });
    if (expectedFollowId == null) {
      await waitForEvent(events);
      assert.equal(events.length, 1, `${name} must not emit an extra acceptance`);
      assert.equal(events[0]?.delivery_id, controlId,
        `${name} must not change a subscription before the valid control`);
    } else {
      await waitForEvent(events);
      assert.equal(events.length, 1);
      assert.equal(events[0]?.event_type, "follow.accepted");
      assert.equal(events[0]?.actor_id, COMMUNITY);
      assert.equal((events[0]?.object as Record<string, unknown>).follow_activity_id, expectedFollowId);
    }
  } finally {
    queueStop.abort();
    await worker?.catch(() => {});
    restoreFetch();
  }
}

/** Wrap a concrete bridge Follow in the community's stable Accept ID. */
function makeAccept(
  followId: string | null,
  acceptId: string,
  objectType: string,
  options: { asUrl?: boolean; followActor?: string; followTarget?: string } = {},
): Record<string, unknown> {
  return {
    "@context": "https://www.w3.org/ns/activitystreams",
    type: "Accept", id: acceptId, actor: COMMUNITY,
    object: options.asUrl ? followId : {
      type: objectType, ...(followId == null ? {} : { id: followId }),
      actor: options.followActor ?? `${ORIGIN}actors/services/bridge`,
      object: options.followTarget ?? COMMUNITY,
    },
  };
}

/** Sign the actual HTTP body with the community's test key. */
async function signedAcceptRequest(activity: Record<string, unknown>, privateKey: CryptoKey): Promise<Request> {
  const body = JSON.stringify(activity);
  const bytes = new TextEncoder().encode(body);
  return await signRequest(new Request(`${ORIGIN}inbox`, {
    method: "POST",
    headers: { "Content-Type": "application/activity+json", Accept: "application/activity+json" },
    body,
  }), privateKey, new URL(`${COMMUNITY}#main-key`), {
    body: bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
  });
}

/** Serve only the remote signer and capture the actual Python event HTTP call. */
function installFixtureFetch(publicKeyPem: string, events: Array<Record<string, unknown>>): () => void {
  const original = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const url = input instanceof URL ? input.href : typeof input === "string" ? input : input.url;
    if (url.startsWith("http://python-accept.fixture/")) {
      if (url.endsWith("/internal/activitypub/events")) {
        events.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
        return jsonResponse({ status: "processed", outcome: "delivered" });
      }
      return jsonResponse({ detail: "not found" }, 404);
    }
    if (url in preloadedContexts) return jsonResponse(preloadedContexts[url]);
    if (url === COMMUNITY || url === `${COMMUNITY}#main-key`) {
      return jsonResponse({
        "@context": ["https://www.w3.org/ns/activitystreams", "https://w3id.org/security/v1"],
        type: "Group", id: COMMUNITY, inbox: `${COMMUNITY}/inbox`,
        publicKey: { id: `${COMMUNITY}#main-key`, owner: COMMUNITY, publicKeyPem },
      });
    }
    return jsonResponse({ detail: "not found" }, 404);
  };
  return () => { globalThis.fetch = original; };
}

/** Return a JSON-LD fixture response without using the network. */
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

/** Wait for bridge delivery; an inbox HTTP 202 alone is not success. */
async function waitForEvent(events: Array<Record<string, unknown>>): Promise<void> {
  const deadline = Date.now() + 6000;
  while (events.length === 0 && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.ok(events.length > 0, "signed Accept must reach the Python boundary");
}

await main();
