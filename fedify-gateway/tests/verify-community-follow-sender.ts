/**
 * Regression: outbound Follow resolves the registered service actor in Fedify.
 *
 * A test-only sendActivity stub cannot expose username-to-identifier lookup.
 * This scenario uses a real Fedify context, the same typed mapping/key
 * dispatchers as the gateway, stored service keys, and immediate HTTP
 * delivery. Only the remote Group HTTP endpoint is replaced by a fake.
 */
import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { createFederation, exportJwk, MemoryKvStore } from "@fedify/fedify";
import { Application, Follow } from "@fedify/vocab";

import { buildServiceFollowActivityId } from "../src/activitypub-identity-urls.js";
import { loadActorKeyPair } from "../src/actor-store.js";
import { followCommunity, sendPreparedCommunityFollow } from "../src/outbound/community-follow.js";
import type { GatewayConfig } from "../src/config.js";
import initSqlJs, { seedBridgeActorJwk } from "./support/sqlite-fixture.js";
import { closeAllPythonBridgeFixtures, importFixedRsaKeyPair, startPythonBridgeFixture } from "./support/python-bridge-fixture.js";

const ORIGIN = "https://bridge.example/";
const GROUP = "https://nodebb.example/category/42";
const INBOX = "https://nodebb.example/category/42/inbox";
const SERVICE = `${ORIGIN}actors/services/bridge`;

/** Run both Follow senders with Fedify's actual sender resolution and keys. */
async function main(): Promise<void> {
  const directory = await mkdtemp(path.join(tmpdir(), "bridge-follow-sender-"));
  const originalFetch = globalThis.fetch;
  try {
    const sqlJs = await initSqlJs();
    const database = new sqlJs.Database();
    const keys = await importFixedRsaKeyPair();
    seedBridgeActorJwk(
      database,
      SERVICE,
      JSON.stringify(await exportJwk(keys.privateKey)),
      JSON.stringify(await exportJwk(keys.publicKey)),
    );
    const file = path.join(directory, "bridge.db");
    await writeFile(file, Buffer.from(database.export()));
    database.close();
    const pythonBridgeInternalUrl = await startPythonBridgeFixture(file);
    const config: GatewayConfig = {
      fedifyOrigin: ORIGIN,
      actorIdentifier: "bridge",
      actorName: "Bridge",
      actorSummary: "Bridge actor",
      pythonBridgeInternalUrl,
      pythonBridgeSharedSecret: "test-secret",
      port: 3000,
      logLevel: "info",
    };
    // No queue: the test must observe the real signed HTTP POST rather than
    // interpreting admission to an in-memory queue as successful delivery.
    const federation = createFederation<GatewayConfig>({
      origin: ORIGIN, kv: new MemoryKvStore(),
    });
    federation
      .setActorDispatcher("/actors/{+identifier}", (ctx, identifier) =>
        identifier === "services/bridge"
          ? new Application({ id: ctx.getActorUri(identifier), inbox: ctx.getInboxUri(identifier) })
          : null)
      .setKeyPairsDispatcher(async (_ctx, identifier) => {
        const pair = await loadActorKeyPair(config, identifier);
        return pair == null ? [] : [pair];
      })
      .mapHandle((_ctx, username) => username === "bridge" ? "services/bridge" : null);
    const deliveries: Array<{ actor: string; object: string; id: string; headers: Headers }> = [];

    // Real Python fixture requests use loopback. A fake remote actor/inbox is
    // the only network seam; sending to any other external URL is an error.
    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.startsWith("http://127.0.0.1:")) return await originalFetch(input, init);
      const method = init?.method ?? (input instanceof Request ? input.method : "GET");
      if (url === GROUP && method === "GET") {
        return new Response(JSON.stringify({ id: GROUP, inbox: INBOX, type: "Group" }), {
          headers: { "content-type": "application/activity+json" },
        });
      }
      if (url === INBOX && method === "POST") {
        const request = new Request(input, init);
        const body = await request.json() as Record<string, unknown>;
        deliveries.push({
          actor: String(body.actor), object: String(body.object), id: String(body.id),
          headers: request.headers,
        });
        return new Response("", { status: 202 });
      }
      throw new Error(`unexpected external fetch: ${method} ${url}`);
    };

    const reservedId = buildServiceFollowActivityId(ORIGIN, "bridge", "real-fedify-test").href;
    const ctx = federation.createContext(new URL(ORIGIN), config);
    // Reproduce the original failure through Fedify itself, without a stubbed
    // sendActivity: a typed identifier cannot be mapped as a username.
    await assert.rejects(() => ctx.sendActivity(
      { username: "services/bridge" },
      { id: new URL(GROUP), inboxId: new URL(INBOX) },
      new Follow({ id: new URL(reservedId), actor: new URL(SERVICE), object: new URL(GROUP) }),
    ), /No actor found for the given username/);
    assert.equal(deliveries.length, 0);

    await sendPreparedCommunityFollow(federation, config, {
      communityActorUrl: GROUP, communityInboxUrl: INBOX, followActivityId: reservedId,
    });
    assert.equal(deliveries.length, 1, "prepared Follow must reach remote HTTP");
    assert.equal(deliveries[0]?.id, reservedId);

    // The operational bootstrap path must use the same service identifier.
    const bootstrap = await followCommunity(federation, config, GROUP);
    assert.equal(deliveries.length, 2, "bootstrap Follow must reach remote HTTP");
    assert.equal(deliveries[1]?.id, bootstrap.followActivityId);
    for (const delivery of deliveries) {
      assert.equal(delivery.actor, SERVICE);
      assert.equal(delivery.object, GROUP);
      assert.ok(delivery.headers.has("signature") || delivery.headers.has("signature-input"),
        "Fedify must HTTP-sign Follow with the service actor's stored key");
    }
  } finally {
    globalThis.fetch = originalFetch;
    await closeAllPythonBridgeFixtures();
    await rm(directory, { recursive: true, force: true });
  }
  console.log("verify:community-follow-sender passed");
}

await main();
