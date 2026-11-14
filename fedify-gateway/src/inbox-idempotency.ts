/** Keep Fedify's normal per-inbox dedup except for signed NodeBB announcements.
 *
 * Repeated Group announcements must reach Python to finish provably missing
 * Discord targets. Fedify still verifies the HTTP signature before it calls
 * an inbox listener, and Python still enforces the Follow, object owner,
 * immutable Create snapshot and per-target CAS. This override does not
 * authorize a new publisher or turn unknown sends into safe retries.
 */

interface InboxIdentity {
  readonly origin: string | URL;
  readonly recipient: string | null;
}

interface ActivityIdentity {
  readonly id: URL | null;
  readonly actorId: URL | null;
}

/** Return null only for NodeBB-shaped Group Announce, retaining per-inbox keys elsewhere. */
export function gatewayInboxIdempotencyKey(
  ctx: InboxIdentity,
  activity: ActivityIdentity,
  isAnnounce: boolean,
): string | null {
  const id = activity.id;
  if (id == null) return null; // Activities without a stable ID cannot be deduplicated safely.
  if (isAnnounce && activity.actorId != null) {
    const actor = activity.actorId;
    // Path matching is only a routing hint: Fedify verifies the signed request,
    // then Python proves the accepted NodeBB Group and immutable object owner.
    if (actor.protocol === "https:" && /^\/category\/\d+$/.test(actor.pathname)
        && actor.search === "" && actor.hash === "") {
      return null;
    }
  }
  // Preserve Fedify 2.x "per-inbox" identity (origin + actual inbox owner).
  const inboxId = ctx.recipient == null ? "shared" : `actor\n${ctx.recipient}`;
  return `${ctx.origin}\n${id.href}\n${inboxId}`;
}
