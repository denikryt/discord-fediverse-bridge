import { webcrypto } from "node:crypto";

import {
  importJwk,
} from "@fedify/fedify";

import { buildActorAddress, parseLocalActorUrl } from "./activitypub-identity-urls.js";
import type { GatewayConfig } from "./config.js";
import { parseTypedActorIdentifier } from "./typed-actor-routing.js";
import {
  loadBridgeActorKey,
  loadLocalCommunityByActorUrl,
  loadLocalCommunityBySlug,
  loadRegisteredUserByUsername,
  type LocalCommunityRow,
  type RegisteredUserRow,
} from "./python-bridge-client.js";

export interface BridgeActorIdentity {
  // The bridge actor is config-backed because the deployment has exactly one
  // stable service actor and does not need a DB row to describe it.
  actorId: URL;
  identifier: string;
  inboxId: URL;
  outboxId: URL;
  followersId: URL;
  name: string;
  summary: string;
}

export interface UserActorIdentity {
  // Registered users are DB-backed because registration owns the actor URLs and
  // PEM key material written during the Python-side signup flow.
  actorId: URL;
  followersId: URL;
  inboxId: URL;
  outboxId: URL;
  publicKeyPem: string;
  privateKeyPem: string;
  username: string;
  displayName: string;
  summary: string | null;
  fields: { name: string; value: string }[];
  avatar: { url: URL; contentType: string; width: number; height: number } | null;
  header: { url: URL; contentType: string; width: number; height: number } | null;
}

export interface LocalCommunityIdentity {
  // Local communities are Group actors backed by Discord forums and stored in
  // the shared Python-owned database.
  actorId: URL;
  followersId: URL;
  inboxId: URL;
  outboxId: URL;
  publicKeyPem: string;
  privateKeyPem: string;
  slug: string;
  displayName: string;
  summary: string;
}

export type LocalActorKind = "service" | "user" | "community";


export function getBridgeActorIdentity(
  config: GatewayConfig,
): BridgeActorIdentity {
  const address = buildActorAddress(config.fedifyOrigin, "service", config.actorIdentifier);
  return {
    actorId: address.actor,
    identifier: config.actorIdentifier,
    inboxId: address.inbox,
    outboxId: address.outbox,
    followersId: address.followers,
    name: config.actorName,
    summary: config.actorSummary,
  };
}

export async function loadUserActorIdentity(
  config: GatewayConfig,
  username: string,
): Promise<UserActorIdentity | null> {
  const row = await loadRegisteredUserByUsername(config, username);
  if (row == null) {
    return null;
  }
  return mapRegisteredUserRow(row);
}

export async function loadLocalCommunityIdentity(
  config: GatewayConfig,
  slug: string,
): Promise<LocalCommunityIdentity | null> {
  const row = await loadLocalCommunityBySlug(config, slug);
  if (row == null) {
    return null;
  }
  return mapLocalCommunityRow(row);
}

export async function hasLocalActor(
  config: GatewayConfig,
  identifier: string,
): Promise<boolean> {
  return (await resolveLocalActorKind(config, identifier)) != null;
}

export async function resolveLocalActorKind(
  config: GatewayConfig,
  identifier: string,
): Promise<LocalActorKind | null> {
  const typed = parseTypedActorIdentifier(identifier);
  if (typed == null) return null;
  if (typed.kind === "service") {
    return typed.identifier === config.actorIdentifier ? "service" : null;
  }
  if (typed.kind === "user") {
    return (await loadRegisteredUserByUsername(config, typed.identifier)) == null ? null : "user";
  }
  return (await loadLocalCommunityBySlug(config, typed.identifier)) == null ? null : "community";
}

export async function loadActorKeyPair(
  config: GatewayConfig,
  identifier: string,
): Promise<CryptoKeyPair | null> {
  const typed = parseTypedActorIdentifier(identifier);
  if (typed == null) return null;
  if (typed.kind === "service") {
    return typed.identifier === config.actorIdentifier ? await loadBridgeActorKeyPair(config) : null;
  }
  if (typed.kind === "user") {
    const user = await loadUserActorIdentity(config, typed.identifier);
    return user == null ? null : await importPemKeyPair(user.publicKeyPem, user.privateKeyPem);
  }
  const community = await loadLocalCommunityIdentity(config, typed.identifier);
  return community == null ? null : await importPemKeyPair(community.publicKeyPem, community.privateKeyPem);
}

async function loadBridgeActorKeyPair(
  config: GatewayConfig,
): Promise<CryptoKeyPair> {
  // The bridge actor key is durable Python-owned state. Missing storage is a
  // startup/migration error rather than a reason to create a temporary identity.
  const row = await loadBridgeActorKey(config);
  if (row.algorithm !== "RSASSA-PKCS1-v1_5") {
    throw new Error(`Unsupported bridge actor key algorithm: ${row.algorithm}`);
  }
  if (row.keyFormat === "jwk") {
    return {
      privateKey: await importJwk(JSON.parse(row.privateKeyData), "private"),
      publicKey: await importJwk(JSON.parse(row.publicKeyData), "public"),
    };
  }
  return await importPemKeyPair(row.publicKeyData, row.privateKeyData);
}

function mapRegisteredUserRow(row: RegisteredUserRow): UserActorIdentity {
  const actorId = new URL(row.actorUrl);
  // Registered users are loaded from the already-migrated typed actor URL.
  // The gateway derives collection URLs from that row so it cannot accidentally
  // reintroduce the retired untyped actor namespace.
  return {
    actorId,
    followersId: new URL("followers", `${actorId.href.replace(/\/$/, "")}/`),
    inboxId: new URL("inbox", `${actorId.href.replace(/\/$/, "")}/`),
    outboxId: new URL("outbox", `${actorId.href.replace(/\/$/, "")}/`),
    publicKeyPem: row.publicKeyPem,
    privateKeyPem: row.privateKeyPem,
    username: row.activitypubUsername,
    displayName: row.displayName,
    summary: row.summary,
    fields: row.fields,
    avatar: row.avatar == null ? null : { ...row.avatar, url: new URL(row.avatar.url) },
    header: row.header == null ? null : { ...row.header, url: new URL(row.header.url) },
  };
}

function mapLocalCommunityRow(row: LocalCommunityRow): LocalCommunityIdentity {
  return {
    actorId: new URL(row.actorUrl),
    followersId: new URL(row.followersUrl),
    inboxId: new URL(row.inboxUrl),
    outboxId: new URL(row.outboxUrl),
    publicKeyPem: row.publicKeyPem,
    privateKeyPem: row.privateKeyPem,
    slug: row.slug,
    displayName: row.displayName,
    summary: row.summary,
  };
}

export async function resolveLocalCommunityByActorUrl(
  config: GatewayConfig,
  actorUrl: string,
): Promise<LocalCommunityIdentity | null> {
  const parsed = parseLocalActorUrl(config.fedifyOrigin, actorUrl);
  if (parsed?.kind === "community") {
    return await loadLocalCommunityIdentity(config, parsed.identifier);
  }
  const row = await loadLocalCommunityByActorUrl(config, actorUrl);
  if (row == null) {
    return null;
  }
  return mapLocalCommunityRow(row);
}

async function importPemKeyPair(
  publicKeyPem: string,
  privateKeyPem: string,
): Promise<CryptoKeyPair> {
  return {
    privateKey: await webcrypto.subtle.importKey(
      "pkcs8",
      pemToDer(privateKeyPem),
      {
        name: "RSASSA-PKCS1-v1_5",
        hash: "SHA-256",
      },
      true,
      ["sign"],
    ) as unknown as CryptoKey,
    publicKey: await webcrypto.subtle.importKey(
      "spki",
      pemToDer(publicKeyPem),
      {
        name: "RSASSA-PKCS1-v1_5",
        hash: "SHA-256",
      },
      true,
      ["verify"],
    ) as unknown as CryptoKey,
  };
}

function pemToDer(pem: string): ArrayBuffer {
  // PEM decoding is isolated here because the shared DB stores text PEM values
  // while Fedify expects WebCrypto key objects.
  const normalized = pem
    .replace(/-----BEGIN [^-]+-----/g, "")
    .replace(/-----END [^-]+-----/g, "")
    .replace(/\s+/g, "");
  const bytes = Buffer.from(normalized, "base64");
  return bytes.buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength,
  );
}
