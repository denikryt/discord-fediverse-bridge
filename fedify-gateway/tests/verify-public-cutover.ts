/**
 * Guards the typed ActivityPub identity migration public typed namespace cutover.
 *
 * typed ActivityPub identity migration must switch production Fedify routing and public ActivityPub route
 * source away from old compatibility families in the same repository state as
 * Python durable identity migration.
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

/** Verify production source exposes the typed typed ActivityPub identity migration public contract. */
async function main(): Promise<void> {
  const federation = await readFile(new URL("../src/federation.ts", import.meta.url), "utf8");
  const webfinger = await readFile(new URL("../src/webfinger.ts", import.meta.url), "utf8");
  const actorStore = await readFile(new URL("../src/actor-store.ts", import.meta.url), "utf8");
  const publicRoutes = await readFile(new URL("../src/routes/public-activitypub.ts", import.meta.url), "utf8");

  assert.match(federation, /\.setActorDispatcher\("\/actors\/\{\+identifier\}"/);
  assert.match(federation, /\.setInboxListeners\("\/actors\/\{\+identifier\}\/inbox", "\/inbox"\)/);
  assert.doesNotMatch(federation, /\.setActorDispatcher\("\/actors\/\{identifier\}"/);
  assert.doesNotMatch(federation, /\.setInboxListeners\("\/communities\/\{identifier\}\/inbox"/);

  assert.match(actorStore, /parseTypedActorIdentifier\(/);
  assert.match(actorStore, /buildActorAddress\(config\.fedifyOrigin, "service"/);
  assert.doesNotMatch(actorStore, /actorId: new URL\(`\/actors\/\$\{config\.actorIdentifier\}`/);

  assert.doesNotMatch(webfinger, /new URL\(`\/c\/\$\{/);

  for (const route of [
    'app.get("/actors/users/:username/outbox"',
    'app.get("/actors/users/:username/followers"',
    'app.get("/actors/communities/:slug/outbox"',
    'app.get("/actors/communities/:slug/followers"',
    'app.get("/actors/services/:identifier/outbox"',
    'app.get("/actors/users/:username/objects/posts/:objectId"',
    'app.get("/actors/communities/:slug/objects/posts/:objectId"',
  ]) {
    assert.ok(publicRoutes.includes(route), `typed ActivityPub identity migration typed route missing: ${route}`);
  }

  for (const oldRoute of [
    'app.get("/users/:username"',
    'app.get("/communities/:slug"',
    'app.get("/c/:slug"',
    'app.get("/actors/:identifier/outbox"',
  ]) {
    assert.ok(!publicRoutes.includes(oldRoute), `Old AP route still registered: ${oldRoute}`);
  }

  console.log("verify:typed ActivityPub identity migration-public-cutover passed");
}

await main();
