/** Verify the gateway reads the canonical project version. */

import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import packageJson from "../package.json" with { type: "json" };
import { APP_VERSION, readProjectVersion } from "../src/project-version.js";
import { createGatewayApp } from "../src/server.js";
import type { GatewayConfig } from "../src/config.js";

const rootVersion = readFileSync(resolve(import.meta.dirname, "../../VERSION"), "utf8").trim();
assert.equal(APP_VERSION, rootVersion);
assert.equal(packageJson.version, rootVersion);

const temporaryDirectory = mkdtempSync(join(tmpdir(), "bridge-version-"));
const emptyVersionPath = join(temporaryDirectory, "VERSION");
writeFileSync(emptyVersionPath, "\n", "utf8");
assert.throws(() => readProjectVersion(emptyVersionPath), /version file is empty/);
assert.throws(() => readProjectVersion(join(temporaryDirectory, "missing")), /version file is missing/);

const config: GatewayConfig = {
  actorIdentifier: "bridge",
  actorName: "Bridge",
  actorSummary: "Bridge test actor",
  fedifyOrigin: "https://bridge.example.com/",
  port: 3000,
  pythonBridgeInternalUrl: "http://127.0.0.1:8080",
  pythonBridgeSharedSecret: "secret",
  logLevel: "info",
};
const response = await createGatewayApp(config).request("/healthz");
assert.equal(response.status, 200);
assert.deepEqual(await response.json(), { status: "ok", version: rootVersion });

console.log("project version contract verified");
