type RawEntry = {
  rawJson: unknown;
  rawBodySha256?: string;
  storedAt: number;
};

// Announce handling may happen after the original HTTP request context is gone,
// so we keep a short-lived lookup from activity id to raw inbox payload.
const rawByActivityId = new Map<string, RawEntry>();
const MAX_ENTRIES = 512;
const TTL_MS = 15 * 60 * 1000;

function prune(now: number): void {
  // Keep the cache bounded in both age and entry count so it stays a transient
  // delivery aid rather than a growing process store.
  for (const [activityId, entry] of rawByActivityId) {
    if (now - entry.storedAt > TTL_MS) {
      rawByActivityId.delete(activityId);
    }
  }

  while (rawByActivityId.size > MAX_ENTRIES) {
    const oldestKey = rawByActivityId.keys().next().value;
    if (!oldestKey) {
      break;
    }
    rawByActivityId.delete(oldestKey);
  }
}

export function storeRawActivity(
  activityId: string,
  rawJson: unknown,
  rawBodySha256?: string,
): void {
  const now = Date.now();
  prune(now);
  rawByActivityId.set(activityId, {
    rawJson,
    rawBodySha256,
    storedAt: now,
  });
}

export function getRawActivity(
  activityId: string,
): { rawJson: unknown; rawBodySha256?: string } | null {
  const entry = rawByActivityId.get(activityId);
  if (!entry) {
    return null;
  }
  if (Date.now() - entry.storedAt > TTL_MS) {
    rawByActivityId.delete(activityId);
    return null;
  }
  return {
    rawJson: entry.rawJson,
    rawBodySha256: entry.rawBodySha256,
  };
}
