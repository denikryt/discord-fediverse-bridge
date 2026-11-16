/**
 * Outbound user-owned Person Follow transport.
 *
 * Python commits the exact sender, remote Person, inbox and Follow ID before
 * the gateway may sign. This boundary never changes the recipient or retries
 * an ambiguous network outcome with a newly invented activity identity.
 */
import { Follow, Person } from "@fedify/vocab";
import type { Federation } from "@fedify/fedify";

import type { GatewayConfig } from "../config.js";
import { loadActorKeyPair } from "../actor-store.js";
import { buildActorAddress, buildUserFollowActivityId } from "../activitypub-identity-urls.js";
import { admitRemoteDeliveryEndpoint } from "../remote-delivery-endpoints.js";

/** Gateway-side typed identity and endpoint data for one committed Follow. */
export interface PreparedUserPersonFollow {
  actorUsername: string;
  actorUrl: string;
  remoteActorId: string;
  remoteInboxUrl: string;
  followActivityId: string;
}

/** Prove that a local prerequisite failed before any outbound HTTP request. */
export class UserFollowSigningUnavailableError extends Error {
  constructor() {
    super("User Person signing key unavailable");
    this.name = "UserFollowSigningUnavailableError";
  }
}

/** Resolve an account handle or canonical URL to a validated remote Person. */
export async function resolveRemotePersonForFollow(
  federation: Federation<GatewayConfig>, config: GatewayConfig, identifier: string,
): Promise<{ actorId: string; inboxUrl: string }> {
  const trimmed = identifier.trim();
  const match = trimmed.match(/^@?([^\s@:/]+)@([^\s@:/]+)$/);
  let lookup = trimmed;
  if (match != null) {
    // Host admission precedes WebFinger, which enforces its own private-DNS
    // and redirect policy during resolution.
    admitRemoteDeliveryEndpoint(`https://${match[2]}/`, { localOrigin: config.fedifyOrigin });
    lookup = `@${match[1]}@${match[2]}`;
  } else {
    lookup = admitRemoteDeliveryEndpoint(trimmed, { localOrigin: config.fedifyOrigin });
  }
  const ctx = federation.createContext(new URL(config.fedifyOrigin), config);
  const actor = await ctx.lookupObject(lookup);
  if (!(actor instanceof Person) || actor.id == null || actor.inboxId == null) {
    throw new Error("resolved object must be a Person with a canonical id and inbox");
  }
  const actorId = admitRemoteDeliveryEndpoint(actor.id.href, { localOrigin: config.fedifyOrigin });
  const inboxUrl = admitRemoteDeliveryEndpoint(actor.inboxId.href, { localOrigin: config.fedifyOrigin });
  if (match == null && actorId !== lookup) {
    throw new Error("requested Actor URL differs from the canonical Person identity");
  }
  return { actorId, inboxUrl };
}

/** Validate immutable actor ownership and signer independent of remote delivery. */
export function validatePreparedUserPersonFollow(
  config: GatewayConfig, prepared: PreparedUserPersonFollow,
): { sender: URL; recipient: URL; inbox: URL; followId: URL; senderTail: string } {
  const expectedSender = buildActorAddress(config.fedifyOrigin, "user", prepared.actorUsername).actor;
  if (expectedSender.href !== prepared.actorUrl) {
    throw new Error("prepared Follow sender is not the local user's canonical Person");
  }
  const recipient = new URL(admitRemoteDeliveryEndpoint(prepared.remoteActorId, { localOrigin: config.fedifyOrigin }));
  const inbox = new URL(admitRemoteDeliveryEndpoint(prepared.remoteInboxUrl, { localOrigin: config.fedifyOrigin }));
  const followId = new URL(prepared.followActivityId);
  const prefix = buildUserFollowActivityId(config.fedifyOrigin, prepared.actorUsername, "placeholder").href.replace(/placeholder$/, "");
  if (followId.origin !== expectedSender.origin || !followId.href.startsWith(prefix) ||
      followId.pathname.length <= new URL(prefix).pathname.length || followId.search || followId.hash) {
    throw new Error("Follow activity ID must be reserved under its Person actor");
  }
  return { sender: expectedSender, recipient, inbox, followId, senderTail: `users/${prepared.actorUsername}` };
}

/** Sign and deliver only the already committed per-user Follow tuple. */
export async function sendPreparedUserPersonFollow(
  federation: Federation<GatewayConfig>, config: GatewayConfig, prepared: PreparedUserPersonFollow,
): Promise<void> {
  const verified = validatePreparedUserPersonFollow(config, prepared);
  // A missing owner key is the only safe local retry condition. Other errors
  // may already have reached the remote inbox and remain uncertain in Python.
  try {
    if (await loadActorKeyPair(config, verified.senderTail) == null) {
      throw new UserFollowSigningUnavailableError();
    }
  } catch {
    throw new UserFollowSigningUnavailableError();
  }
  const context = federation.createContext(new URL(config.fedifyOrigin), config);
  const activity = new Follow({ id: verified.followId, actor: verified.sender, object: verified.recipient });
  await context.sendActivity(
    { identifier: verified.senderTail },
    { id: verified.recipient, inboxId: verified.inbox },
    activity,
    { immediate: true },
  );
}
