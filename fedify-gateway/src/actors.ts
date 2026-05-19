import {
  type CryptographicKey,
  Endpoints,
  Group,
  OrderedCollection,
  Person,
  Service,
} from "@fedify/vocab";

import type {
  BridgeActorIdentity,
  LocalCommunityIdentity,
  UserActorIdentity,
} from "./actor-store.js";

interface PublicKeyCarrier {
  // Fedify enriches actor key pairs with a `cryptographicKey` object that can
  // be embedded directly into actor documents.
  cryptographicKey: CryptographicKey;
}

export function buildBridgeServiceActor(
  identity: BridgeActorIdentity,
  sharedInboxId: URL,
  keyPairs: PublicKeyCarrier[],
): Service {
  // The service actor stays intentionally small because later stages only need
  // a correct identity envelope for follow and inbound community handling.
  return new Service({
    id: identity.actorId,
    preferredUsername: identity.identifier,
    name: identity.name,
    summary: identity.summary,
    inbox: identity.inboxId,
    outbox: identity.outboxId,
    followers: identity.followersId,
    endpoints: new Endpoints({
      sharedInbox: sharedInboxId,
    }),
    publicKeys: keyPairs.map((keyPair) => keyPair.cryptographicKey),
  });
}

export function buildUserPersonActor(
  identity: UserActorIdentity,
  sharedInboxId: URL,
  keyPairs: PublicKeyCarrier[],
): Person {
  // Registered Discord users are represented as Person actors because they own
  // their own local AP identity and later author outbound Create activities.
  return new Person({
    id: identity.actorId,
    preferredUsername: identity.username,
    name: identity.username,
    inbox: identity.inboxId,
    outbox: identity.outboxId,
    followers: identity.followersId,
    endpoints: new Endpoints({
      sharedInbox: sharedInboxId,
    }),
    publicKeys: keyPairs.map((keyPair) => keyPair.cryptographicKey),
  });
}

export function buildLocalCommunityGroupActor(
  identity: LocalCommunityIdentity,
  sharedInboxId: URL,
  keyPairs: PublicKeyCarrier[],
): Group {
  // Local communities are represented as Group actors because they model one
  // shared discussion space while leaving authorship to individual user actors.
  return new Group({
    id: identity.actorId,
    preferredUsername: identity.slug,
    name: identity.displayName,
    summary: identity.summary,
    inbox: identity.inboxId,
    outbox: identity.outboxId,
    followers: identity.followersId,
    endpoints: new Endpoints({
      sharedInbox: sharedInboxId,
    }),
    publicKeys: keyPairs.map((keyPair) => keyPair.cryptographicKey),
  });
}

export function buildEmptyOrderedCollection(collectionId: URL): OrderedCollection {
  // Stage 3 only needs stable collection URLs. Returning an empty collection
  // keeps the surface coherent until follow/outbox semantics arrive later.
  return new OrderedCollection({
    id: collectionId,
    totalItems: 0,
    items: [],
  });
}
