import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath, pathToFileURL } from "node:url";

/**
 * Verify that gateway commands load shared deployment values from the root env
 * file and gateway-local values from fedify-gateway/.env.
 */
async function main(): Promise<void> {
  const testsDir = path.dirname(fileURLToPath(import.meta.url));
  const gatewayDir = path.resolve(testsDir, "..");
  const tempDir = await mkdtemp(path.join(tmpdir(), "fedify-env-loading-"));
  const rootEnvPath = path.join(tempDir, "root.env");
  const gatewayEnvPath = path.join(tempDir, "gateway.env");
  const probePath = path.join(tempDir, "probe.ts");

  // Root env owns deployment-wide values consumed by both processes.
  await writeFile(
    rootEnvPath,
    [
      "FEDIFY_ORIGIN=https://root.example.com",
      "FEDIFY_SHARED_SECRET=root-secret",
      "LOG_LEVEL=debug",
      "DATABASE_URL=sqlite:///./root-bridge.db",
      "",
    ].join("\n"),
  );

  // Gateway env owns only gateway-local overrides and the gateway DB URL.
  await writeFile(
    gatewayEnvPath,
    [
      "DATABASE_URL=sqlite:///../gateway-bridge.db",
      "FEDIFY_PORT=4100",
      'FEDIFY_BRIDGE_PRIVATE_KEY_JWK_JSON={"kty":"RSA","d":"private"}',
      'FEDIFY_BRIDGE_PUBLIC_KEY_JWK_JSON={"kty":"RSA","n":"public"}',
      "FEDIFY_ACTOR_IDENTIFIER=test-bridge",
      "FEDIFY_ACTOR_NAME=Test Bridge",
      "FEDIFY_ACTOR_SUMMARY=Test summary",
      "",
    ].join("\n"),
  );

  // Spawn the same Node + tsx shape used by package.json so this test protects
  // the actual operator-facing env-loading contract.
  await writeFile(
    probePath,
    [
      `import { loadConfig } from ${JSON.stringify(pathToFileURL(path.resolve(gatewayDir, "src/config.ts")).href)};`,
      "console.log(JSON.stringify(loadConfig()));",
      "",
    ].join("\n"),
  );

  const result = spawnSync(
    process.execPath,
    [
      "--env-file",
      rootEnvPath,
      "--env-file",
      gatewayEnvPath,
      "./node_modules/.bin/tsx",
      probePath,
    ],
    {
      cwd: gatewayDir,
      encoding: "utf8",
      // Start from a clean env so the spawned process only sees the temporary
      // env files under test instead of inheriting values from npm test.
      env: {
        HOME: process.env.HOME ?? "",
        PATH: process.env.PATH ?? "",
        TMPDIR: process.env.TMPDIR ?? "",
      },
    },
  );

  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(result.stderr || result.stdout);
  }

  const config = JSON.parse(result.stdout.trim()) as Record<string, unknown>;
  assert.equal(config.fedifyOrigin, "https://root.example.com");
  assert.equal(config.pythonBridgeSharedSecret, "root-secret");
  assert.equal(config.logLevel, "debug");
  assert.equal(config.databaseUrl, "sqlite:///../gateway-bridge.db");
  assert.equal(config.port, 4100);
  assert.equal(config.actorIdentifier, "test-bridge");
  assert.equal(config.actorName, "Test Bridge");
  assert.equal(config.actorSummary, "Test summary");
  assert.equal(
    config.bridgePrivateKeyJwkJson,
    '{"kty":"RSA","d":"private"}',
  );
  assert.equal(
    config.bridgePublicKeyJwkJson,
    '{"kty":"RSA","n":"public"}',
  );

  console.log("verify:env-loading passed");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
