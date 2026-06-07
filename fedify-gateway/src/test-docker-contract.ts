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
assert.match(compose, /DATABASE_URL: sqlite:\/\/\/\/data\/bridge\.db/);
console.log("Docker gateway contract verified.");
