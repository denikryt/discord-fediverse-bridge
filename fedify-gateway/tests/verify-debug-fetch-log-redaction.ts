/**
 * Gateway debug fetch logging redaction verification.
 *
 * The debug fetch logger sees authenticated Python bridge requests made from
 * public routes. It must preserve useful request diagnostics without persisting
 * the Python bridge bearer secret in JSONL logs.
 */
import assert from "node:assert/strict";

import { headersToRecord } from "../src/debug-file-log.js";

function main(): void {
  const redacted = headersToRecord({
    Authorization: "Bearer super-secret",
    "Proxy-Authorization": "Basic other-secret",
    Cookie: "session=secret",
    "Set-Cookie": "session=secret",
    Accept: "application/activity+json",
  });

  assert.equal(redacted.Authorization, "[redacted]");
  assert.equal(redacted["Proxy-Authorization"], "[redacted]");
  assert.equal(redacted.Cookie, "[redacted]");
  assert.equal(redacted["Set-Cookie"], "[redacted]");
  assert.equal(redacted.Accept, "application/activity+json");
  assert.equal(JSON.stringify(redacted).includes("super-secret"), false);
  assert.equal(JSON.stringify(redacted).includes("other-secret"), false);

  const tupleHeaders = headersToRecord([
    ["authorization", "Bearer tuple-secret"],
    ["x-debug", "kept"],
  ]);
  assert.equal(tupleHeaders.authorization, "[redacted]");
  assert.equal(tupleHeaders["x-debug"], "kept");

  console.log("verify:debug-fetch-log-redaction passed");
}

main();
