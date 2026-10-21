/** Verify the gateway container contract without requiring a Docker daemon. */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "../..");
const dockerfile = await readFile(resolve(root, "fedify-gateway/Dockerfile"), "utf8");
const compose = await readFile(resolve(root, "compose.yaml"), "utf8");

assert.match(dockerfile, /COPY VERSION \.\/VERSION/);
assert.match(dockerfile, /USER bridge/);
assert.match(dockerfile, /EXPOSE 3000/);
assert.match(compose, /BRIDGE_EVENTS_URL: http:\/\/bridge:8080\/internal\/activitypub\/events/);
const gatewayBlock = compose.split("  fedify-gateway:")[1]?.split("\n  backup:")[0] ?? "";
assert.doesNotMatch(gatewayBlock, /DATABASE_URL/);
assert.doesNotMatch(gatewayBlock, /bridge-data:\/data/);
// The public-facing process must receive only secrets and settings it uses.
// `env_file` would silently inject Discord OAuth/bot secrets into the gateway.
assert.doesNotMatch(gatewayBlock, /^    env_file:/m);
for (const key of ["PUBLIC_BASE_URL", "FEDIFY_SHARED_SECRET", "FEDIFY_ACTOR_IDENTIFIER", "FEDIFY_ACTOR_NAME", "FEDIFY_ACTOR_SUMMARY", "LOG_LEVEL", "GATEWAY_BIND_PORT", "BRIDGE_EVENTS_URL"]) {
  assert.match(gatewayBlock, new RegExp(`^      ${key}:`, "m"));
}
assert.match(gatewayBlock, /^      PUBLIC_BASE_URL: \$\{PUBLIC_BASE_URL:\?required\}/m);
assert.match(gatewayBlock, /^      FEDIFY_SHARED_SECRET: \$\{FEDIFY_SHARED_SECRET:\?required\}/m);
for (const unwanted of ["DISCORD_TOKEN", "DISCORD_OAUTH_CLIENT_SECRET", "DATABASE_URL", "BRIDGE_BIND_HOST", "BRIDGE_BIND_PORT", "PROFILE_MEDIA_ROOT"]) {
  assert.doesNotMatch(gatewayBlock, new RegExp(`^      ${unwanted}:`, "m"));
}
// Python still needs the selected file to receive its Discord credentials.
const bridgeBlock = compose.split("  bridge:")[1]?.split("\n  fedify-gateway:")[0] ?? "";
assert.match(bridgeBlock, /^    env_file:/m);
console.log("Docker gateway contract verified.");
