/**
 * Contract tests for the gateway's raw signed JSON ActivityPub transport.
 *
 * The transport is the security-sensitive seam shared by outbound use cases
 * that already have rendered ActivityPub JSON. These tests pin the wire shape
 * before the Stage 1 module split so refactoring cannot change signatures,
 * digests, request-target construction, or redaction behavior accidentally.
 */

import assert from "node:assert/strict";
import { createHash } from "node:crypto";

import { importFixedRsaKeyPair } from "./support/python-bridge-fixture.js";
import { sendSignedJsonActivity } from "../src/outbound/signed-json-delivery.js";

interface CapturedRequest {
  url: string;
  method: string | undefined;
  body: string;
  headers: Record<string, string>;
}

/** Run the transport contract scenarios. */
async function main(): Promise<void> {
  await testSignedJsonHeadersAndRequestTarget();
  await testHttpFailureRedactsResponseBodyWhenDisabled();
  console.log("verify:signed-json-delivery passed");
}

/**
 * Action: rendered ActivityPub JSON is delivered to an inbox URL with a query.
 * Expected: the body digest and HTTP Signature metadata match the exact body
 * bytes and the signed request-target includes both path and query string.
 */
async function testSignedJsonHeadersAndRequestTarget(): Promise<void> {
  const keyPair = await importFixedRsaKeyPair();
  const activityJson = {
    "@context": "https://www.w3.org/ns/activitystreams",
    type: "Create",
    id: "https://bridge.example/activities/1",
    actor: "https://bridge.example/actors/alice",
  };
  const captured: CapturedRequest[] = [];
  const rawLogs: unknown[][] = [];
  const restoreFetch = installFetch(captured, () => new Response("accepted", { status: 202 }));
  const restoreConsole = captureConsoleLog(rawLogs);

  try {
    await sendSignedJsonActivity(
      "TransportTest",
      "https://remote.example/inbox?x=1",
      { keyId: new URL("https://bridge.example/actors/alice#main-key"), privateKey: keyPair.privateKey },
      activityJson,
      { debugDelivery: true },
    );
  } finally {
    restoreConsole();
    restoreFetch();
  }

  assert.equal(captured.length, 1);
  const request = captured[0];
  assert.ok(request != null);
  assert.equal(request.url, "https://remote.example/inbox?x=1");
  assert.equal(request.method, "POST");
  assert.equal(request.body, JSON.stringify(activityJson));
  assert.equal(request.headers["Content-Type"], "application/activity+json");
  assert.equal(
    request.headers.Digest,
    `SHA-256=${createHash("sha256").update(request.body).digest("base64")}`,
  );
  assert.equal(request.headers.Host, "remote.example");
  assert.match(request.headers.Date ?? "", /^[A-Z][a-z]{2}, /);
  assert.ok(
    request.headers.Signature?.includes('keyId="https://bridge.example/actors/alice#main-key"'),
  );
  assert.ok(request.headers.Signature?.includes('algorithm="rsa-sha256"'));
  assert.ok(
    request.headers.Signature?.includes('headers="(request-target) host date digest content-type"'),
  );
  assert.ok(request.headers.Signature?.includes('signature="'));
  assert.equal(
    rawLogs.some((entry) => String((entry[1] as Record<string, unknown> | undefined)?.signingString ?? "").includes("(request-target): post /inbox?x=1")),
    true,
  );
}

/**
 * Action: an inbox rejects the signed request while response-body diagnostics
 * are disabled.
 * Expected: the thrown HTTP error preserves the status but does not leak the
 * response body into caller-visible retry diagnostics.
 */
async function testHttpFailureRedactsResponseBodyWhenDisabled(): Promise<void> {
  const keyPair = await importFixedRsaKeyPair();
  const restoreFetch = installFetch([], () => new Response("SECRET_RESPONSE_BODY", { status: 503 }));
  const rawLogs: unknown[][] = [];
  const restoreConsole = captureConsoleLog(rawLogs);

  try {
    await assert.rejects(
      () => sendSignedJsonActivity(
        "TransportTest",
        "https://remote.example/inbox",
        { keyId: new URL("https://bridge.example/actors/alice#main-key"), privateKey: keyPair.privateKey },
        { type: "Update", id: "https://bridge.example/activities/2" },
        {
          debugDelivery: false,
          includeResponseBodyInDiagnostics: false,
        },
      ),
      (error: unknown) => {
        assert.ok(error instanceof Error);
        assert.equal(error.message, "TransportTest signed JSON delivery failed with HTTP 503");
        assert.equal(error.message.includes("SECRET_RESPONSE_BODY"), false);
        return true;
      },
    );
  } finally {
    restoreConsole();
    restoreFetch();
  }
  assert.equal(
    rawLogs.some((entry) => JSON.stringify(entry).includes("SECRET_RESPONSE_BODY")),
    false,
  );
}

/** Install a fetch fake that records the transport-visible request. */
function installFetch(
  captured: CapturedRequest[],
  responseFactory: () => Response,
): () => void {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const url = input instanceof URL ? input.href : typeof input === "string" ? input : input.url;
    captured.push({
      url,
      method: init?.method,
      body: String(init?.body ?? ""),
      headers: init?.headers as Record<string, string>,
    });
    return responseFactory();
  };
  return () => {
    globalThis.fetch = originalFetch;
  };
}

await main();

/** Capture console.log without hiding the transport behavior under test. */
function captureConsoleLog(logs: unknown[][]): () => void {
  const originalConsoleLog = console.log;
  console.log = (...args: unknown[]) => {
    logs.push(args);
  };
  return () => {
    console.log = originalConsoleLog;
  };
}
