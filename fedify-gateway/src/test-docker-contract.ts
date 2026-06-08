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
console.log("Docker gateway contract verified.");
