/**
 * Signed ActivityPub JSON HTTP transport for already-rendered activities.
 *
 * This module owns the exact digest, HTTP Signature header, request logging,
 * and failure redaction contract. It deliberately does not load keys, render
 * domain activities, select recipients, or perform durable retry.
 */

import { createHash, webcrypto } from "node:crypto";

import type { GatewayConfig } from "../config.js";

export interface ActivitySigningKey {
  keyId: URL;
  privateKey: CryptoKey;
}

export interface SignedJsonDeliveryOptions {
  debugDelivery: boolean;
  includeRequestDetailsInDiagnostics?: boolean;
  includeResponseBodyInDiagnostics?: boolean;
}

/** Sign and POST already-rendered ActivityPub JSON to one inbox. */
export async function sendSignedJsonActivity(
  label: string,
  inboxUrl: string,
  signingKey: ActivitySigningKey,
  activityJson: Record<string, unknown>,
  options: SignedJsonDeliveryOptions,
): Promise<void> {
  // Local-community fanout uses an exact JSON wire contract that mirrors
  // Lemmy's Announce(Create(...)) shape. Fedify's high-level sendActivity
  // expects Fedify-native activity objects, so this path signs and posts the
  // already-rendered JSON directly while keeping detailed body logs opt-in.
  const inbox = new URL(inboxUrl);
  const body = JSON.stringify(activityJson);
  const digest = `SHA-256=${createHash("sha256").update(body).digest("base64")}`;
  const date = new Date().toUTCString();
  const contentType = "application/activity+json";
  const requestTarget = `post ${inbox.pathname}${inbox.search}`;
  const signingString = [
    `(request-target): ${requestTarget}`,
    `host: ${inbox.host}`,
    `date: ${date}`,
    `digest: ${digest}`,
    `content-type: ${contentType}`,
  ].join("\n");
  const signatureBytes = await webcrypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    signingKey.privateKey,
    Buffer.from(signingString),
  );
  const signature = Buffer.from(signatureBytes).toString("base64");
  const signatureHeader = [
    `keyId="${signingKey.keyId.href}"`,
    `algorithm="rsa-sha256"`,
    `headers="(request-target) host date digest content-type"`,
    `signature="${signature}"`,
  ].join(",");
  const headers = {
    Host: inbox.host,
    Date: date,
    Digest: digest,
    "Content-Type": contentType,
    Signature: signatureHeader,
  };

  if (options.debugDelivery) {
    if (options.includeRequestDetailsInDiagnostics === false) {
      console.log(`[${label}] ActivityPub request:`, {
        inboxUrl,
        activityId:
          typeof activityJson.id === "string" ? activityJson.id : null,
      });
    } else {
      console.log(`[${label}] Raw ActivityPub request:`, {
        inboxUrl,
        body,
        headers,
        signingString,
      });
    }
  }
  const response = await fetch(inbox, { method: "POST", headers, body });
  const responseBody = await response.text();
  if (options.debugDelivery) {
    console.log(`[${label}] Raw ActivityPub response:`, {
      inboxUrl,
      status: response.status,
      statusText: response.statusText,
      ...(options.includeResponseBodyInDiagnostics === false ? {} : { responseBody }),
    });
  }
  if (!response.ok) {
    const suffix = options.includeResponseBodyInDiagnostics === false
      ? ""
      : `: ${responseBody}`;
    throw new Error(
      `${label} signed JSON delivery failed with HTTP ${response.status}${suffix}`,
    );
  }
}

/** Return whether operator-configured debug logs should include transport diagnostics. */
export function shouldLogSignedJsonDelivery(config: GatewayConfig): boolean {
  // LOG_LEVEL=debug is the single operator switch for verbose gateway
  // diagnostics, including signed JSON delivery request/response logging.
  return config.logLevel === "debug";
}
