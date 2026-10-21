/**
 * Signed ActivityPub JSON HTTP transport for already-rendered activities.
 *
 * This module owns the exact digest, HTTP Signature header, request logging,
 * and failure redaction contract. It deliberately does not load keys, render
 * domain activities, select recipients, or perform durable retry.
 */

import { createHash, webcrypto } from "node:crypto";
import { request as httpsRequest } from "node:https";

import { admitRemoteDeliveryEndpoint, publicOnlyLookup } from "../remote-delivery-endpoints.js";

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
  // Reject literal private destinations before signing or opening a socket.
  const inbox = new URL(admitRemoteDeliveryEndpoint(inboxUrl));
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
  const response = await sendViaValidatedSocket(inbox, headers, body);
  const responseBody = response.body;
  if (options.debugDelivery) {
    console.log(`[${label}] Raw ActivityPub response:`, {
      inboxUrl,
      status: response.status,
      statusText: response.statusText,
      ...(options.includeResponseBodyInDiagnostics === false ? {} : { responseBody }),
    });
  }
  if (response.status < 200 || response.status >= 300) {
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


/** Send exactly one HTTPS POST using DNS resolution validated at socket dial. */
function sendViaValidatedSocket(
  inbox: URL,
  headers: Record<string, string>,
  body: string,
): Promise<{ status: number; statusText: string; body: string }> {
  // Native https.request does not follow 3xx redirects: the signature, body,
  // and key ID are never replayed to an unvalidated second destination.
  return new Promise((resolve, reject) => {
    const request = httpsRequest(inbox, {
      method: "POST",
      headers,
      lookup: publicOnlyLookup,
      agent: false,
      rejectUnauthorized: true,
      timeout: 10_000,
    }, (response) => {
      const chunks: Buffer[] = [];
      let responseLength = 0;
      response.on("data", (chunk: Buffer) => {
        responseLength += chunk.length;
        if (responseLength > 65_536) {
          request.destroy(new Error("remote ActivityPub response exceeds size limit"));
        } else {
          chunks.push(chunk);
        }
      });
      response.on("end", () => resolve({
        status: response.statusCode ?? 0,
        statusText: response.statusMessage ?? "",
        body: Buffer.concat(chunks).toString("utf8"),
      }));
      response.on("error", reject);
    });
    request.on("timeout", () => request.destroy(new Error("remote ActivityPub request timed out")));
    request.on("error", reject);
    request.end(body);
  });
}
