/**
 * Canonical local ActivityPub actor, object, and activity URL builders.
 *
 * typed actor grammar uses this pure module as the TypeScript target grammar for later
 * routing and migration work. It intentionally imports no gateway runtime,
 * signing, network, Python-client, or config code so secret-bearing settings do
 * not cross into identity construction.
 */

export type LocalActorKind = "service" | "user" | "community";

export interface ActorAddress {
  /** Local actor category encoded in the typed actor path. */
  kind: LocalActorKind;
  /** Decoded local identifier owned by the corresponding actor category. */
  identifier: string;
  /** Canonical actor id URL. */
  actor: URL;
  /** Actor-specific inbox URL. */
  inbox: URL;
  /** Actor outbox URL. */
  outbox: URL;
  /** Actor followers collection URL. */
  followers: URL;
  /** Main public key id under the actor URL. */
  publicKey: URL;
}

const KIND_TO_SEGMENT: Record<LocalActorKind, string> = {
  service: "services",
  user: "users",
  community: "communities",
};

const SEGMENT_TO_KIND: Record<string, LocalActorKind | undefined> = {
  services: "service",
  users: "user",
  communities: "community",
};

const USERNAME_PATTERN = /^[a-z0-9_-]+$/;
const COMMUNITY_SLUG_PATTERN = /^[a-z0-9_-]+$/;

function normalizeOrigin(origin: string | URL): string {
  /** Validate and canonicalize the ActivityPub origin root. */
  const parsed = origin instanceof URL ? origin : new URL(origin);
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    throw new Error("origin must be an absolute http(s) origin");
  }
  if (parsed.username || parsed.password) {
    throw new Error("origin must not contain userinfo");
  }
  if (parsed.pathname !== "/") {
    throw new Error("origin must not contain a non-root path");
  }
  if (parsed.search || parsed.hash) {
    throw new Error("origin must not contain query or fragment components");
  }
  return parsed.origin;
}

function validateIdentifier(kind: LocalActorKind, identifier: string): void {
  /** Preserve current per-kind local actor naming rules. */
  if (kind === "user") {
    if (!USERNAME_PATTERN.test(identifier)) {
      throw new Error("user identifier must match existing username policy");
    }
    return;
  }
  if (kind === "community") {
    if (!COMMUNITY_SLUG_PATTERN.test(identifier)) {
      throw new Error("community identifier must match existing slug policy");
    }
    return;
  }
  if (!identifier || identifier.includes("/") || identifier.includes("?") || identifier.includes("#")) {
    throw new Error("service identifier must be one non-empty path segment");
  }
}

function segment(value: string | number): string {
  /** Encode one non-empty logical segment before appending it to a URL path. */
  const text = String(value);
  if (text === "") {
    throw new Error("path segment must not be empty");
  }
  return encodeURIComponent(text);
}

function actorRoot(origin: string | URL, kind: LocalActorKind, identifier: string): URL {
  /** Build the encoded actor root with an absolute path to avoid prefix leaks. */
  validateIdentifier(kind, identifier);
  return new URL(`/actors/${KIND_TO_SEGMENT[kind]}/${segment(identifier)}`, normalizeOrigin(origin));
}

/** Build the canonical typed address family for one local actor. */
export function buildActorAddress(
  origin: string | URL,
  kind: LocalActorKind,
  identifier: string,
): ActorAddress {
  const actor = actorRoot(origin, kind, identifier);
  return {
    kind,
    identifier,
    actor,
    inbox: new URL(`${actor.pathname}/inbox`, actor),
    outbox: new URL(`${actor.pathname}/outbox`, actor),
    followers: new URL(`${actor.pathname}/followers`, actor),
    publicKey: new URL(`${actor.href}#main-key`),
  };
}

/** Classify a same-origin typed actor URL without guessing legacy paths. */
export function parseLocalActorUrl(
  origin: string | URL,
  actorUrl: string | URL,
): ActorAddress | null {
  let normalizedOrigin: string;
  let parsed: URL;
  try {
    normalizedOrigin = normalizeOrigin(origin);
    parsed = actorUrl instanceof URL ? actorUrl : new URL(actorUrl);
  } catch {
    return null;
  }
  if (
    parsed.origin !== normalizedOrigin ||
    parsed.search ||
    parsed.hash ||
    parsed.username ||
    parsed.password
  ) {
    return null;
  }
  const parts = parsed.pathname.split("/");
  if (parts.length !== 4 || parts[0] !== "" || parts[1] !== "actors") {
    return null;
  }
  const kind = SEGMENT_TO_KIND[parts[2]];
  if (!kind || !parts[3]) {
    return null;
  }
  let identifier: string;
  try {
    identifier = decodeURIComponent(parts[3]);
  } catch {
    return null;
  }
  // Encoded slash ambiguity is rejected so a single actor identifier cannot be
  // interpreted as nested route state by a different router layer.
  if (identifier.includes("/")) {
    return null;
  }
  try {
    return buildActorAddress(normalizedOrigin, kind, identifier);
  } catch {
    return null;
  }
}

function userRoot(origin: string | URL, username: string): URL {
  /** Return the canonical typed root for one local user actor. */
  return actorRoot(origin, "user", username);
}

function communityRoot(origin: string | URL, slug: string): URL {
  /** Return the canonical typed root for one local community actor. */
  return actorRoot(origin, "community", slug);
}

function serviceRoot(origin: string | URL, identifier: string): URL {
  /** Return the canonical typed root for one local service actor. */
  return actorRoot(origin, "service", identifier);
}

function childUrl(root: URL, suffix: string): URL {
  /** Append a canonical suffix using an absolute path under the validated origin. */
  return new URL(`${root.pathname}${suffix}`, root);
}

/** Build a typed local user post object id. */
export function buildUserPostObjectId(origin: string | URL, username: string, objectId: string): URL {
  return childUrl(userRoot(origin, username), `/objects/posts/${segment(objectId)}`);
}

/** Build a typed local user comment object id. */
export function buildUserCommentObjectId(origin: string | URL, username: string, objectId: string): URL {
  return childUrl(userRoot(origin, username), `/objects/comments/${segment(objectId)}`);
}

/** Build a typed local user Create(Post) activity id. */
export function buildUserCreatePostActivityId(origin: string | URL, username: string, activityId: string): URL {
  return childUrl(userRoot(origin, username), `/activities/create/posts/${segment(activityId)}`);
}

/** Build a typed local user Create(Comment) activity id. */
export function buildUserCreateCommentActivityId(origin: string | URL, username: string, activityId: string): URL {
  return childUrl(userRoot(origin, username), `/activities/create/comments/${segment(activityId)}`);
}

/** Build a typed local user Update(Post) activity id. */
export function buildUserUpdatePostActivityId(origin: string | URL, username: string, activityId: string): URL {
  return childUrl(userRoot(origin, username), `/activities/update/posts/${segment(activityId)}`);
}

/** Build a typed local user Update(Comment) activity id. */
export function buildUserUpdateCommentActivityId(origin: string | URL, username: string, activityId: string): URL {
  return childUrl(userRoot(origin, username), `/activities/update/comments/${segment(activityId)}`);
}

/** Build a typed local user Delete(Post) activity id. */
export function buildUserDeletePostActivityId(origin: string | URL, username: string, activityId: string): URL {
  return childUrl(userRoot(origin, username), `/activities/delete/posts/${segment(activityId)}`);
}

/** Build a typed local user Delete(Comment) activity id. */
export function buildUserDeleteCommentActivityId(origin: string | URL, username: string, activityId: string): URL {
  return childUrl(userRoot(origin, username), `/activities/delete/comments/${segment(activityId)}`);
}

/** Build a typed local user Accept(Follow) activity id. */
export function buildUserAcceptFollowActivityId(origin: string | URL, username: string, activityId: string): URL {
  return childUrl(userRoot(origin, username), `/activities/accept/follows/${segment(activityId)}`);
}

/** Build a typed local user profile Update activity id. */
export function buildUserProfileUpdateActivityId(
  origin: string | URL,
  username: string,
  profileVersionOrActivityId: string | number,
): URL {
  return childUrl(userRoot(origin, username), `/activities/update/profile/${segment(profileVersionOrActivityId)}`);
}

/** Build a typed local community post object id. */
export function buildCommunityPostObjectId(origin: string | URL, slug: string, objectId: string): URL {
  return childUrl(communityRoot(origin, slug), `/objects/posts/${segment(objectId)}`);
}

/** Build a typed local community comment object id. */
export function buildCommunityCommentObjectId(origin: string | URL, slug: string, objectId: string): URL {
  return childUrl(communityRoot(origin, slug), `/objects/comments/${segment(objectId)}`);
}

/** Build a typed local community Create(Post) activity id. */
export function buildCommunityCreatePostActivityId(origin: string | URL, slug: string, activityId: string): URL {
  return childUrl(communityRoot(origin, slug), `/activities/create/posts/${segment(activityId)}`);
}

/** Build a typed local community Create(Comment) activity id. */
export function buildCommunityCreateCommentActivityId(origin: string | URL, slug: string, activityId: string): URL {
  return childUrl(communityRoot(origin, slug), `/activities/create/comments/${segment(activityId)}`);
}

/** Build a typed local community Announce(Post) activity id. */
export function buildCommunityAnnouncePostActivityId(origin: string | URL, slug: string, activityId: string): URL {
  return childUrl(communityRoot(origin, slug), `/activities/announce/posts/${segment(activityId)}`);
}

/** Build a typed local community Announce(Comment) activity id. */
export function buildCommunityAnnounceCommentActivityId(origin: string | URL, slug: string, activityId: string): URL {
  return childUrl(communityRoot(origin, slug), `/activities/announce/comments/${segment(activityId)}`);
}

/** Build a typed local community Update(Post) activity id. */
export function buildCommunityUpdatePostActivityId(origin: string | URL, slug: string, activityId: string): URL {
  return childUrl(communityRoot(origin, slug), `/activities/update/posts/${segment(activityId)}`);
}

/** Build a typed local community Update(Comment) activity id. */
export function buildCommunityUpdateCommentActivityId(origin: string | URL, slug: string, activityId: string): URL {
  return childUrl(communityRoot(origin, slug), `/activities/update/comments/${segment(activityId)}`);
}

/** Build a typed local community Delete(Post) activity id. */
export function buildCommunityDeletePostActivityId(origin: string | URL, slug: string, activityId: string): URL {
  return childUrl(communityRoot(origin, slug), `/activities/delete/posts/${segment(activityId)}`);
}

/** Build a typed local community Delete(Comment) activity id. */
export function buildCommunityDeleteCommentActivityId(origin: string | URL, slug: string, activityId: string): URL {
  return childUrl(communityRoot(origin, slug), `/activities/delete/comments/${segment(activityId)}`);
}

/** Build a typed local community Accept(Follow) activity id. */
export function buildCommunityAcceptFollowActivityId(origin: string | URL, slug: string, activityId: string): URL {
  return childUrl(communityRoot(origin, slug), `/activities/accept/follows/${segment(activityId)}`);
}

/** Build a typed local service actor Follow activity id. */
export function buildServiceFollowActivityId(origin: string | URL, identifier: string, activityId: string): URL {
  return childUrl(serviceRoot(origin, identifier), `/activities/follow/${segment(activityId)}`);
}

/** Build a typed local service actor Undo(Follow) activity id. */
export function buildServiceUndoFollowActivityId(origin: string | URL, identifier: string, activityId: string): URL {
  return childUrl(serviceRoot(origin, identifier), `/activities/undo/follows/${segment(activityId)}`);
}
