/**
 * JSON-LD normalization for outbound ActivityPub activities.
 *
 * The gateway keeps this serializer separate from delivery so use cases can
 * produce canonical JSON without loading signing keys or selecting recipients.
 */

const ACTIVITYSTREAMS_PUBLIC_IRI = "https://www.w3.org/ns/activitystreams#Public";

export type ActivityJsonValue = Record<string, unknown> | unknown[] | string | number | boolean | null;

/** Render one Fedify activity to normalized public ActivityPub JSON. */
export async function renderPublicActivityJson(activity: { toJsonLd(): Promise<unknown> }): Promise<Record<string, unknown>> {
  // Fedify's JSON-LD serializer may compact the public collection as
  // `as:Public`. Lemmy's local-community validation rejected that compact IRI
  // as `object_is_not_public`, so local-community fanout expands it before
  // signing or sending the activity.
  return normalizeActivityPubJson(await activity.toJsonLd() as ActivityJsonValue) as Record<string, unknown>;
}

/** Normalize compact public IRIs and addressing fields without changing policy. */
export function normalizeActivityPubJson(value: ActivityJsonValue, key?: string): ActivityJsonValue {
  // ActivityPub addressing fields are scalar-or-array in the vocabulary, but
  // using arrays consistently keeps the wire shape close to Lemmy's own
  // Announce(Create(...)) activities and avoids target-specific ambiguity.
  if (typeof value === "string") {
    const expanded = value === "as:Public" ? ACTIVITYSTREAMS_PUBLIC_IRI : value;
    if ((key === "to" || key === "cc" || key === "bto" || key === "bcc") && expanded !== value) {
      return [expanded];
    }
    return expanded;
  }

  if (Array.isArray(value)) {
    return value.map((item) => normalizeActivityPubJson(item as ActivityJsonValue));
  }

  if (value != null && typeof value === "object") {
    const normalized: Record<string, unknown> = {};
    for (const [entryKey, entryValue] of Object.entries(value)) {
      const nextValue = normalizeActivityPubJson(entryValue as ActivityJsonValue, entryKey);
      if ((entryKey === "to" || entryKey === "cc" || entryKey === "bto" || entryKey === "bcc") && nextValue != null && !Array.isArray(nextValue)) {
        normalized[entryKey] = [nextValue];
      } else {
        normalized[entryKey] = nextValue;
      }
    }
    return normalized;
  }

  return value;
}
