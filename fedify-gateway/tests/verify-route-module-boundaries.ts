/**
 * Gateway route-module boundary verification.
 *
 * Stage 2 keeps public HTTP request parsing separate from secret-bearing
 * composition. This test guards the file-level boundary so future route edits
 * cannot accidentally regain direct access to GatewayConfig, Fedify context
 * creation, or the Python bridge shared secret.
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const PUBLIC_ROUTE_FILES = [
  "src/routes/public-activitypub.ts",
  "src/routes/public-discovery.ts",
  "src/routes/inbox-logging.ts",
  "src/routes/route-responses.ts",
];

const FORBIDDEN_PUBLIC_PATTERNS = [
  "GatewayConfig",
  "GatewayContextData",
  "loadConfig",
  "createGatewayFederation",
  "pythonBridgeSharedSecret",
  "createContext",
  "from \"../python-bridge-client.js\"",
  "from '../python-bridge-client.js'",
];

async function main(): Promise<void> {
  for (const file of PUBLIC_ROUTE_FILES) {
    const source = await readFile(new URL(`../${file}`, import.meta.url), "utf8");
    for (const pattern of FORBIDDEN_PUBLIC_PATTERNS) {
      assert.equal(
        source.includes(pattern),
        false,
        `${file} must not contain ${pattern}`,
      );
    }
  }

  console.log("verify:route-module-boundaries passed");
}

await main();
