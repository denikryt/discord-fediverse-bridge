/**
 * Test-only remote ActivityPub actor server for outer-boundary dereference.
 *
 * The fixture serves Bob's actor document over normal HTTP and captures inbox
 * POSTs. Tests use the returned URLs exactly as a remote server would expose
 * them, avoiding patches to Fedify vocabulary objects or normalizers.
 */
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { Socket } from "node:net";
import type { DocumentLoader } from "@fedify/vocab";
import { preloadedContexts } from "@fedify/vocab-runtime";

export interface RemoteActorFixtureOptions {
  /** Override actor document id to exercise hostile identity binding. */
  actorDocumentId?: string;
  /** Override actor document inbox to exercise endpoint admission. */
  inboxUrl?: string;
  /** Override actor document sharedInbox to exercise endpoint admission. */
  sharedInboxUrl?: string | null;
}

export interface RemoteActorFixture {
  /** Origin of the ephemeral fake remote server. */
  origin: string;
  /** Bob's ActivityPub actor id. */
  actorId: string;
  /** Bob's actor-specific inbox URL. */
  inboxUrl: string;
  /** Bob's shared inbox URL. */
  sharedInboxUrl: string;
  /** Bob's public key id. */
  publicKeyId: string;
  /** Captured POST request bodies by pathname. */
  capturedPosts: Map<string, string[]>;
  /** HTTP-backed document loader for Fedify vocabulary dereference. */
  documentLoader: DocumentLoader;
  /** Close server and any open sockets owned by this fixture. */
  close(): Promise<void>;
}

const FIXTURE_PUBLIC_KEY_PEM = `-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEApZIweMLb8KgJmYd5om7X
P6ojbn+mqi8mzQki+Q/YWeJNPf2/x/9ec9ZTXf41wNQ+e3vk708jCdKcMHk7MRYk
SA06E0IdcW29JLPp98MwwwZWG19n7w7rHVblAqIl3CiPaBwCLRy2P1j7LI/woE+0
yrANCKPA+zDAAKejHMUVxMb/wA4kcOqamy6uRphekhp1Deuppmu1k0cHDz426+6j
F3KrgDIyqiGgO9Eju4Ivf9JbW09g/0cjToNzcXONqLAJHOAP0j0wHUSFmaQpfubd
Nkk9+xXkl/FfiJKdkJbIg7wHU33rDw+V3UyamaobsjeLfUgHdgl9bWjcIvyi2zpe
eQIDAQAB
-----END PUBLIC KEY-----`;

/** Start Bob's fake remote actor document and inbox endpoints. */
export async function startRemoteActorFixture(options: RemoteActorFixtureOptions = {}): Promise<RemoteActorFixture> {
  const capturedPosts = new Map<string, string[]>();
  const sockets = new Set<Socket>();

  const server = createServer(async (request, response) => {
    await routeRemoteActorRequest(request, response, capturedPosts, server, options);
  });
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.once("close", () => sockets.delete(socket));
  });

  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  server.unref();

  const address = server.address();
  if (address == null || typeof address === "string") {
    throw new Error("remote actor fixture did not expose a TCP address");
  }

  const origin = `http://127.0.0.1:${address.port}`;
  return {
    origin,
    actorId: `${origin}/users/bob`,
    inboxUrl: options.inboxUrl ?? `${origin}/users/bob/inbox`,
    sharedInboxUrl: options.sharedInboxUrl ?? `${origin}/inbox`,
    publicKeyId: `${origin}/users/bob#main-key`,
    capturedPosts,
    documentLoader: async (url, options) => {
      // The loader intentionally uses normal fetch against the fixture server;
      // it does not manufacture ActivityPub objects in memory.
      if (url in preloadedContexts) {
        return {
          contextUrl: null,
          document: preloadedContexts[url],
          documentUrl: url,
        };
      }
      const response = await fetch(url, {
        headers: { Accept: "application/activity+json" },
        signal: options?.signal,
      });
      if (!response.ok) {
        throw new Error(`remote actor fixture fetch failed: ${response.status} ${url}`);
      }
      return {
        contextUrl: null,
        document: await response.json(),
        documentUrl: response.url,
      };
    },
    async close() {
      await new Promise<void>((resolve, reject) => {
        server.close((error) => error == null ? resolve() : reject(error));
        for (const socket of sockets) {
          socket.destroy();
        }
        server.closeAllConnections?.();
      });
    },
  };
}

async function routeRemoteActorRequest(
  request: IncomingMessage,
  response: ServerResponse,
  capturedPosts: Map<string, string[]>,
  server: Server,
  options: RemoteActorFixtureOptions,
): Promise<void> {
  const address = server.address();
  if (address == null || typeof address === "string") {
    throw new Error("remote actor fixture lost its TCP address");
  }
  const origin = `http://127.0.0.1:${address.port}`;
  const url = new URL(request.url ?? "/", origin);

  if (request.method === "GET" && url.pathname === "/users/bob") {
    return json(response, buildBobActorDocument(origin, options));
  }

  if (request.method === "POST" && (url.pathname === "/users/bob/inbox" || url.pathname === "/inbox")) {
    const rawBody = await readBody(request);
    const bucket = capturedPosts.get(url.pathname) ?? [];
    bucket.push(rawBody);
    capturedPosts.set(url.pathname, bucket);
    response.statusCode = 202;
    response.end("accepted");
    return;
  }

  response.statusCode = 404;
  response.end("not found");
}

function buildBobActorDocument(origin: string, options: RemoteActorFixtureOptions): Record<string, unknown> {
  const document: Record<string, unknown> = {
    "@context": [
      "https://www.w3.org/ns/activitystreams",
      "https://w3id.org/security/v1",
    ],
    id: options.actorDocumentId ?? `${origin}/users/bob`,
    type: "Person",
    preferredUsername: "bob",
    inbox: options.inboxUrl ?? `${origin}/users/bob/inbox`,
    publicKey: {
      id: `${origin}/users/bob#main-key`,
      owner: options.actorDocumentId ?? `${origin}/users/bob`,
      publicKeyPem: FIXTURE_PUBLIC_KEY_PEM,
    },
  };
  if (options.sharedInboxUrl !== null) {
    document.endpoints = {
      sharedInbox: options.sharedInboxUrl ?? `${origin}/inbox`,
    };
  }
  return document;
}

async function readBody(request: IncomingMessage): Promise<string> {
  let rawBody = "";
  for await (const chunk of request) {
    rawBody += chunk;
  }
  return rawBody;
}

function json(response: ServerResponse, value: unknown): void {
  response.setHeader("Content-Type", "application/activity+json");
  response.setHeader("Cache-Control", "no-store");
  response.end(JSON.stringify(value));
}
