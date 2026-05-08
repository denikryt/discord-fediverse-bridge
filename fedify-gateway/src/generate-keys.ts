import { webcrypto } from "node:crypto";
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";

const { subtle } = webcrypto;

const ENV_FILE = resolve(process.cwd(), ".env");
const PRIVATE_KEY_VAR = "FEDIFY_BRIDGE_PRIVATE_KEY_JWK_JSON";
const PUBLIC_KEY_VAR = "FEDIFY_BRIDGE_PUBLIC_KEY_JWK_JSON";

function readEnv(path: string): string {
  return existsSync(path) ? readFileSync(path, "utf-8") : "";
}

function setEnvVar(content: string, key: string, value: string): string {
  const escaped = value.replace(/\\/g, "\\\\");
  const line = `${key}=${escaped}`;
  const re = new RegExp(`^${key}=.*$`, "m");
  return re.test(content) ? content.replace(re, line) : `${content.trimEnd()}\n${line}\n`;
}

async function main() {
  const existing = readEnv(ENV_FILE);

  if (existing.includes(PRIVATE_KEY_VAR) && existing.includes(PUBLIC_KEY_VAR)) {
    console.log("Keys already present in .env — skipping generation.");
    console.log("Pass --force to regenerate.");
    if (!process.argv.includes("--force") && !process.env.FORCE) {
      process.exit(0);
    }
    console.log("--force passed, regenerating keys...");
  }

  const keyPair = await subtle.generateKey(
    {
      name: "RSASSA-PKCS1-v1_5",
      modulusLength: 2048,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["sign", "verify"],
  );

  const privateJwk = JSON.stringify(
    await subtle.exportKey("jwk", keyPair.privateKey),
  );
  const publicJwk = JSON.stringify(
    await subtle.exportKey("jwk", keyPair.publicKey),
  );

  let updated = setEnvVar(existing, PRIVATE_KEY_VAR, privateJwk);
  updated = setEnvVar(updated, PUBLIC_KEY_VAR, publicJwk);

  writeFileSync(ENV_FILE, updated, "utf-8");

  console.log(`Written to ${ENV_FILE}`);
  console.log(`  ${PRIVATE_KEY_VAR} — set`);
  console.log(`  ${PUBLIC_KEY_VAR}  — set`);
  console.log("Restart the gateway to apply the new keys.");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
