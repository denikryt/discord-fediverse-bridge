/**
 * Fedify typed actor route-tail parsing for the future reserved-expansion route.
 *
 * typed actor route validation keeps this module pure and independent from production actor lookup so
 * typed actor identity migration can parse `/actors/{+identifier}` values before resolving keys,
 * inbox owners, or database-backed actors. The input is the slash-containing
 * identifier tail that Fedify reserved expansion will pass, not a full URL.
 */

export type TypedActorKind = "service" | "user" | "community";

export interface TypedActorIdentifier {
  /** Actor kind selected by the public plural path segment. */
  kind: TypedActorKind;
  /** Local actor identifier after per-kind validation. */
  identifier: string;
  /** Original accepted typed route tail, for example `users/alice`. */
  tail: string;
}

const KIND_SEGMENTS = new Map<string, TypedActorKind>([
  ["services", "service"],
  ["users", "user"],
  ["communities", "community"],
]);

const KIND_TO_SEGMENT: Record<TypedActorKind, string> = {
  service: "services",
  user: "users",
  community: "communities",
};

const USERNAME_PATTERN = /^[a-z0-9_-]+$/;
const COMMUNITY_SLUG_PATTERN = /^[a-z0-9_-]+$/;

/** Parse a Fedify `{+identifier}` typed actor tail without guessing old routes. */
export function parseTypedActorIdentifier(
  tail: string,
): TypedActorIdentifier | null {
  if (!isPlainRouteTail(tail)) {
    return null;
  }

  const parts = tail.split("/");
  if (parts.length !== 2) {
    return null;
  }

  const [kindSegment, identifier] = parts;
  const kind = KIND_SEGMENTS.get(kindSegment);
  if (kind == null || !isValidIdentifier(kind, identifier)) {
    return null;
  }

  // The returned tail is deliberately the original accepted value, not a
  // reconstructed string, so tests can detect accidental decoding or rewriting.
  return { kind, identifier, tail };
}

/** Build a validated slash-containing tail for fixture-only typed routes. */
export function typedActorIdentifierTail(
  kind: TypedActorKind,
  identifier: string,
): string {
  if (!isValidIdentifier(kind, identifier)) {
    throw new Error(`invalid ${kind} actor identifier`);
  }
  return `${KIND_TO_SEGMENT[kind]}/${identifier}`;
}

function isPlainRouteTail(tail: string): boolean {
  // Full paths, URLs, query strings, fragments, empty segments, and encoded
  // slash ambiguity are rejected before the per-kind naming rules run.
  return tail.length > 0
    && !tail.startsWith("/")
    && !tail.includes("?")
    && !tail.includes("#")
    && !tail.includes("://")
    && !tail.split("/").some((segment) => segment.length === 0)
    && !/%2f/i.test(tail);
}

function isValidIdentifier(kind: TypedActorKind, identifier: string): boolean {
  if (kind === "user") {
    return USERNAME_PATTERN.test(identifier);
  }
  if (kind === "community") {
    return COMMUNITY_SLUG_PATTERN.test(identifier);
  }

  // Service identifiers are config-backed names, but still one non-empty path
  // segment without URI-control characters at this public routing boundary.
  return identifier.length > 0
    && !identifier.includes("/")
    && !identifier.includes("?")
    && !identifier.includes("#");
}