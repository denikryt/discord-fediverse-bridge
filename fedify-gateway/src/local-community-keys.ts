/**
 * Local-community key helpers.
 *
 * Fedify derives public-key ownership from the `/actors/{identifier}` dispatcher
 * when callers use `ctx.getActorKeyPairs(identifier)`. Local communities use a
 * database-backed canonical actor URL under `/communities/{slug}`, so this file
 * builds the public actor key and outbound signing key explicitly from that
 * canonical URL. Keeping the override here prevents regular bridge/user actor
 * key behavior from changing.
 */
import { webcrypto } from "node:crypto";

import { CryptographicKey } from "@fedify/vocab";

import type { GatewayConfig } from "./config.js";
import {
  loadLocalCommunityIdentity,
  resolveLocalCommunityByActorUrl,
  type LocalCommunityIdentity,
} from "./actor-store.js";

export interface LocalCommunityPublicKeyCarrier {
  /** Public key object embedded into the ActivityPub Group actor document. */
  cryptographicKey: CryptographicKey;
}

export interface LocalCommunitySigningKey {
  /** Canonical community actor id that owns this signing key. */
  actorId: URL;
  /** HTTP Signature key id that remote servers dereference for verification. */
  keyId: URL;
  /** Private key loaded from the local_communities database row. */
  privateKey: CryptoKey;
}

/**
 * Builds the ActivityPub public key owned by the canonical community actor URL.
 */
export async function buildLocalCommunityPublicKey(
  identity: LocalCommunityIdentity,
): Promise<CryptographicKey> {
  // The key id and owner intentionally derive from identity.actorId, not from
  // Fedify's `/actors/{identifier}` dispatcher route. Mastodon treats that
  // owner relation as part of signature verification for Accept(Follow).
  return new CryptographicKey({
    id: new URL("#main-key", identity.actorId),
    owner: identity.actorId,
    publicKey: await importPublicPemKey(identity.publicKeyPem),
  });
}

/**
 * Returns a key carrier compatible with the actor builders in actors.ts.
 */
export async function buildLocalCommunityPublicKeyCarrier(
  identity: LocalCommunityIdentity,
): Promise<LocalCommunityPublicKeyCarrier[]> {
  return [{ cryptographicKey: await buildLocalCommunityPublicKey(identity) }];
}

/**
 * Loads the canonical outbound signing key for a local community slug.
 */
export async function loadLocalCommunitySigningKey(
  config: GatewayConfig,
  slug: string,
): Promise<LocalCommunitySigningKey | null> {
  const identity = await loadLocalCommunityIdentity(config, slug);
  if (identity == null) {
    return null;
  }
  return await buildLocalCommunitySigningKey(identity);
}

/**
 * Loads the canonical outbound signing key for a local community actor URL.
 */
export async function loadLocalCommunitySigningKeyByActorUrl(
  config: GatewayConfig,
  actorUrl: string,
): Promise<LocalCommunitySigningKey | null> {
  const identity = await resolveLocalCommunityByActorUrl(config, actorUrl);
  if (identity == null) {
    return null;
  }
  return await buildLocalCommunitySigningKey(identity);
}

async function buildLocalCommunitySigningKey(
  identity: LocalCommunityIdentity,
): Promise<LocalCommunitySigningKey> {
  // The private key stays the database keypair already assigned to the local
  // community. Only the ActivityPub key id is canonicalized to the community
  // actor URL that Lemmy and Mastodon follow.
  return {
    actorId: identity.actorId,
    keyId: new URL("#main-key", identity.actorId),
    privateKey: await importPrivatePemKey(identity.privateKeyPem),
  };
}

async function importPrivatePemKey(privateKeyPem: string): Promise<CryptoKey> {
  return await webcrypto.subtle.importKey(
    "pkcs8",
    pemToDer(privateKeyPem),
    {
      name: "RSASSA-PKCS1-v1_5",
      hash: "SHA-256",
    },
    true,
    ["sign"],
  ) as unknown as CryptoKey;
}

async function importPublicPemKey(publicKeyPem: string): Promise<CryptoKey> {
  return await webcrypto.subtle.importKey(
    "spki",
    pemToDer(publicKeyPem),
    {
      name: "RSASSA-PKCS1-v1_5",
      hash: "SHA-256",
    },
    true,
    ["verify"],
  ) as unknown as CryptoKey;
}

function pemToDer(pem: string): ArrayBuffer {
  // PEM values returned by the Python bridge are text envelopes. Fedify
  // and WebCrypto need DER bytes, so the conversion stays local to key loading.
  const base64 = pem
    .replace(/-----BEGIN [^-]+-----/g, "")
    .replace(/-----END [^-]+-----/g, "")
    .replace(/\s+/g, "");
  const bytes = Buffer.from(base64, "base64");
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}
