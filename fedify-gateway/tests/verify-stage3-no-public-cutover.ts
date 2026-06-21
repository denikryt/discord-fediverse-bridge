/**
 * Guards the Stage 3 boundary: helper preparation without public cutover.
 *
 * These source assertions are temporary. Stage 4 should invert or remove them
 * when it atomically migrates production Fedify routing, WebFinger, actor-store
 * lookup, public-key ownership, and Python durable identity rows.
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

/** Verify production source still exposes the pre-cutover public contract. */
async function main(): Promise<void> {
  const federation = await readFile(new URL("../src/federation.ts", import.meta.url), "utf8");
  const webfinger = await readFile(new URL("../src/webfinger.ts", import.meta.url), "utf8");
  const actorStore = await readFile(new URL("../src/actor-store.ts", import.meta.url), "utf8");
  const publicRoutes = await readFile(new URL("../src/routes/public-activitypub.ts", import.meta.url), "utf8");

  assert.match(federation, /\.setActorDispatcher\("\/actors\/\{identifier\}"/);
  assert.match(federation, /\.setInboxListeners\("\/communities\/\{identifier\}\/inbox", "\/inbox"\)/);
  assert.doesNotMatch(federation, /\.setActorDispatcher\("\/actors\/\{\+identifier\}"/);
  assert.doesNotMatch(federation, /\.setInboxListeners\("\/actors\/\{\+identifier\}\/inbox", "\/inbox"\)/);

  assert.match(webfinger, /communityIdentity\.actorId\.href/);
  assert.match(webfinger, /userIdentity\.actorId\.href/);
  assert.doesNotMatch(webfinger, /buildActorAddress\(/);

  assert.match(actorStore, /resolveLocalActorKind/);
  assert.match(actorStore, /loadRegisteredUserByUsername\(config, identifier\)/);
  assert.match(actorStore, /loadLocalCommunityBySlug\(config, identifier\)/);
  assert.doesNotMatch(actorStore, /parseTypedActorIdentifier\(/);

  // Stage 4 must make these old public ActivityPub route families unsupported
  // in the same deployable stage as durable Python identity migration.
  for (const route of [
    'app.get("/users/:username"',
    'app.get("/users/:username/outbox"',
    'app.get("/users/:username/followers"',
    'app.get("/users/:username/activities/create/post/:activityId"',
    'app.get("/users/:username/activities/create/comment/:activityId"',
    'app.get("/users/:username/post/:objectId"',
    'app.get("/users/:username/comment/:objectId"',
    'app.get("/actors/:identifier/outbox"',
    'app.get("/actors/:identifier/followers"',
    'app.get("/communities/:slug"',
    'app.get("/c/:slug"',
    'app.get("/communities/:slug/outbox"',
    'app.get("/communities/:slug/followers"',
    'app.get("/communities/:slug/post/:objectId"',
    'app.get("/communities/:slug/comment/:objectId"',
  ]) {
    assert.ok(publicRoutes.includes(route), `Stage 4 old-route handoff missing: ${route}`);
  }

  console.log("verify:stage3-no-public-cutover passed");
}

await main();
