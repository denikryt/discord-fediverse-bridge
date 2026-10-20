/**
 * Prove authorship of a nested ActivityPub mutation independently of a relay.
 *
 * The outer signed Announce proves only relayer identity. This module compares
 * its nested claim to a freshly dereferenced, author-origin-controlled source
 * before the gateway may normalize that source into a Python event.
 */

/** Minimal Fedify-vetted activity identity; never sourced from the raw cache. */
export interface AcceptedActivityIdentity {
  id: string | null;
  actorId: string | null;
}

/** Outer Announce identity also includes its signed nested activity ID. */
export interface AcceptedAnnounceIdentity extends AcceptedActivityIdentity {
  /** Nested activity ID observed on the authenticated, typed Announce. */
  objectId: string | null;
}

/** Fail closed if an embedded Create/Update/Delete cannot be authenticated at origin. */
export async function verifyForwardedSourceRecord(
  accepted: AcceptedAnnounceIdentity,
  envelope: unknown,
  rawNested: Record<string, unknown>,
  loadOriginActivity: (activityId: string) => Promise<unknown>,
): Promise<Record<string, unknown>> {
  // Raw capture precedes signature verification and is keyed only by the
  // caller-supplied Announce ID. Bind its nested ID to Fedify's accepted typed
  // object before origin lookup; another inbox POST may replace the cache row.
  const raw = record(envelope);
  if (accepted.id == null || accepted.actorId == null || accepted.objectId == null ||
      raw?.type !== "Announce" || rawNested.id !== accepted.objectId ||
      raw.id !== accepted.id || raw.actor !== accepted.actorId ||
      stableJson(raw.object) !== stableJson(rawNested)) {
    throw new Error("ActivityPub Announce cache identity mismatch");
  }

  const kind = rawNested.type;
  const activityId = requiredHttpsId(rawNested.id);
  const author = requiredHttpsId(rawNested.actor);
  if (!(["Create", "Update", "Delete"] as unknown[]).includes(kind)) {
    throw new Error("unsupported forwarded ActivityPub activity type");
  }
  if (new URL(activityId).origin !== new URL(author).origin) {
    throw new Error("forwarded activity ID is not on the claimed author's origin");
  }

  // The caller MUST use Fedify's hardened lookupObject with crossOrigin=throw,
  // never a raw fetch or data supplied by the Announce's sending server.
  const canonical = record(await loadOriginActivity(activityId));
  if (canonical == null) {
    throw new Error("forwarded ActivityPub original origin unverifiable");
  }
  if (canonical.type !== kind || canonical.id !== activityId || canonical.actor !== author) {
    throw new Error("forwarded ActivityPub original identity mismatch");
  }

  const rawObject = kind === "Delete" ? rawNested.object : record(rawNested.object);
  const fetchedObject = kind === "Delete" ? canonical.object : record(canonical.object);
  const objectId = requiredHttpsId(kind === "Delete" ? rawObject : record(rawObject)?.id);
  const canonicalObjectId = requiredHttpsId(kind === "Delete" ? fetchedObject : record(fetchedObject)?.id);
  if (objectId !== canonicalObjectId || new URL(objectId).origin !== new URL(author).origin) {
    throw new Error("forwarded ActivityPub object origin or identity mismatch");
  }
  if (kind !== "Delete") {
    const claimedAuthor = requiredHttpsId(record(rawObject)?.attributedTo);
    const sourceAuthor = requiredHttpsId(record(fetchedObject)?.attributedTo);
    if (claimedAuthor !== author || sourceAuthor !== author) {
      throw new Error("forwarded ActivityPub object creator is not the activity author");
    }
  }

  // Equality binds *meaning*, not JSON object key order. A relayer cannot
  // replace community, recipients, parent, content, timestamps or attachment
  // while retaining a valid outer signature for a different author's record.
  for (const property of ["to", "cc", "audience", "published", "updated"]) {
    if (comparable(rawNested[property]) !== comparable(canonical[property])) {
      throw new Error(`forwarded ActivityPub activity ${property} mismatch`);
    }
  }
  if (kind !== "Delete") {
    for (const property of [
      "type", "name", "summary", "content", "source", "url", "inReplyTo",
      "replyTarget", "audience", "to", "cc", "published", "updated", "attachment",
    ]) {
      if (comparable(record(rawObject)?.[property]) !== comparable(record(fetchedObject)?.[property])) {
        throw new Error(`forwarded ActivityPub object ${property} mismatch`);
      }
    }
  }
  return canonical;
}

/** Require a public-origin HTTPS identifier before attempting dereference. */
function requiredHttpsId(value: unknown): string {
  if (typeof value !== "string") throw new Error("ActivityPub original has missing or ambiguous ID");
  let uri: URL;
  try { uri = new URL(value); } catch { throw new Error("ActivityPub original ID is invalid"); }
  if (uri.protocol !== "https:" || uri.username || uri.password || uri.hash) {
    throw new Error("ActivityPub original ID requires uncredentialed HTTPS without fragment");
  }
  return uri.href;
}

/** Parse only plain-object-shaped JSON documents. */
function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

/** Stable shallow/recursive JSON comparison for author-controlled fields. */
function stableJson(value: unknown): string {
  if (value === undefined) return "undefined";
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  const fields = value as Record<string, unknown>;
  return `{${Object.keys(fields).sort().map(key => `${JSON.stringify(key)}:${stableJson(fields[key])}`).join(",")}}`;
}

/** Canonicalize a timestamp without changing the original activity. */
function comparable(value: unknown): string {
  if (typeof value === "string" && /^\d{4}-\d\d-\d\dT/.test(value)) {
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) return new Date(parsed).toISOString();
  }
  return stableJson(value);
}

/** Check direct Fedify-authenticated activity fields before domain normalization. */
export function verifyDirectSourceAttribution(
  accepted: AcceptedActivityIdentity,
  raw: unknown,
): Record<string, unknown> {
  // Fedify's patched inbox validation authenticates the *direct* actor;
  // object.attributedTo cannot silently designate a different principal.
  const source = record(raw);
  if (source == null || accepted.id == null || accepted.actorId == null ||
      source.id !== accepted.id || source.actor !== accepted.actorId) {
    throw new Error("direct ActivityPub actor identity mismatch");
  }
  const activityId = requiredHttpsId(source.id);
  const actorId = requiredHttpsId(source.actor);
  if (new URL(activityId).origin !== new URL(actorId).origin) {
    throw new Error("direct ActivityPub activity origin mismatch");
  }
  if (source.type === "Create" || source.type === "Update") {
    const object = record(source.object);
    if (object == null || requiredHttpsId(object.attributedTo) !== actorId ||
        new URL(requiredHttpsId(object.id)).origin !== new URL(actorId).origin) {
      throw new Error("direct ActivityPub object attribution mismatch");
    }
  } else if (source.type === "Delete") {
    const objectId = requiredHttpsId(source.object);
    if (new URL(objectId).origin !== new URL(actorId).origin) {
      throw new Error("direct ActivityPub Delete object origin mismatch");
    }
  } else {
    throw new Error("unsupported direct ActivityPub activity type");
  }
  return source;
}
