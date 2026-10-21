/** Capture native HTTPS signed deliveries while preserving the DNS guard contract. */

import { EventEmitter } from "node:events";
import { createRequire, syncBuiltinESMExports } from "node:module";
import type { ClientRequest } from "node:http";
import type { IncomingMessage } from "node:http";
import type { RequestOptions } from "node:https";

import { publicOnlyLookup } from "../../src/remote-delivery-endpoints.js";

const require = createRequire(import.meta.url);

/** The signed request observed at the native HTTPS boundary. */
export interface CapturedSignedRequest {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string;
}

/** Match Fetch Headers' lowercase view used by older delivery assertions. */
export function lowercaseSignedHeaders(headers: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(headers).map(([key, value]) => [key.toLowerCase(), value]));
}

/** Replace only the final HTTPS socket edge in this test process. */
export function installSignedJsonSocketFixture(
  onRequest: (request: CapturedSignedRequest) => { status: number; body?: string },
): () => void {
  const https = require("node:https") as typeof import("node:https");
  const original = https.request;
  // Node's ESM named export is synchronized so the production module's
  // imported httpsRequest binding sees this fixture. No external DNS or dial
  // occurs, while the request still has to supply its public-only lookup.
  (https as { request: typeof original }).request = ((url: URL, options: RequestOptions, callback: (response: IncomingMessage) => void) => {
    if (options.lookup !== publicOnlyLookup || options.rejectUnauthorized !== true) {
      throw new Error("signed delivery lost its validated TLS socket options");
    }
    const request = new EventEmitter() as ClientRequest;
    request.end = ((body: string) => {
      queueMicrotask(() => {
        try {
          const result = onRequest({
            url: url.href,
            method: String(options.method ?? "GET"),
            headers: options.headers as Record<string, string>,
            body,
          });
          const response = new EventEmitter() as IncomingMessage;
          response.statusCode = result.status;
          response.statusMessage = result.status === 202 ? "Accepted" : "Failure";
          callback(response);
          if (result.body != null) response.emit("data", Buffer.from(result.body));
          response.emit("end");
        } catch (error) {
          request.emit("error", error);
        }
      });
      return request;
    }) as ClientRequest["end"];
    return request;
  }) as typeof original;
  syncBuiltinESMExports();
  return () => {
    (https as { request: typeof original }).request = original;
    syncBuiltinESMExports();
  };
}
