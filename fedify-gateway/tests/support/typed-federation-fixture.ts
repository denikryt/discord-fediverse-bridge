/**
 * Test-only Fedify composition for the future typed actor route contract.
 *
 * This fixture proves that `/actors/{+identifier}` can hand slash-containing
 * tails to Stage 3 parsing helpers. It must never be imported by production
 * gateway composition, because Stage 4 owns the real public cutover.
 */
import {
  createFederation,
  MemoryKvStore,
} from "@fedify/fedify";
import { Application, Create, Group, Person } from "@fedify/vocab";

import { parseTypedActorIdentifier, type TypedActorIdentifier } from "../../src/typed-actor-routing.js";

export interface TypedFederationFixture {
  /** Synthetic Fedify federation mounted only inside tests. */
  federation: ReturnType<typeof createFederation<undefined>>;
  /** Dispatcher tails accepted through `/actors/{+identifier}`. */
  actorDispatches: TypedActorIdentifier[];
  /** Inbox tails accepted through `/actors/{+identifier}/inbox` or `/inbox`. */
  inboxDispatches: Array<TypedActorIdentifier | { kind: "shared" }>;
}

/** Build a minimal typed-route Fedify fixture without touching production wiring. */
export function createTypedFederationFixture(
  origin = "https://bridge.example",
): TypedFederationFixture {
  const actorDispatches: TypedActorIdentifier[] = [];
  const inboxDispatches: Array<TypedActorIdentifier | { kind: "shared" }> = [];
  const federation = createFederation<undefined>({
    origin,
    kv: new MemoryKvStore(),
  });

  federation
    .setActorDispatcher("/actors/{+identifier}", (ctx, identifier) => {
      const actor = parseTypedActorIdentifier(identifier);
      if (actor == null) {
        return null;
      }
      actorDispatches.push(actor);
      const actorId = ctx.getActorUri(identifier);
      const inbox = ctx.getInboxUri(identifier);
      if (actor.kind === "user") {
        return new Person({ id: actorId, inbox, preferredUsername: actor.identifier });
      }
      if (actor.kind === "community") {
        return new Group({ id: actorId, inbox, preferredUsername: actor.identifier });
      }
      return new Application({ id: actorId, inbox, name: actor.identifier });
    })
    .setKeyPairsDispatcher(() => []);

  federation
    .setInboxListeners("/actors/{+identifier}/inbox", "/inbox")
    .on(Create, (ctx) => {
      const inbox = typeof ctx.recipient === "string"
        ? parseTypedActorIdentifier(ctx.recipient)
        : { kind: "shared" as const };
      if (inbox != null) {
        inboxDispatches.push(inbox);
      }
    });

  return { federation, actorDispatches, inboxDispatches };
}
