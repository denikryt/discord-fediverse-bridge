/** Select trusted local Group destinations for one Fedify-verified activity.
 *
 * Recipient evidence comes from the verified typed activity serialization and
 * server-owned inbox route, never the pre-verification ID-indexed raw cache.
 * Remote Group distributors retain the separate subscription-based pipeline.
 */

export interface VerifiedInboxRoute {
  kind: "shared" | "actor" | "unknown";
  owner?: { kind: "service" | "user" | "community"; actorUrl: string };
}

export type LocalGroupIngressSelection =
  | { kind: "remote" }
  | { kind: "rejected" }
  | { kind: "selected"; groups: string[] };

const MAX_GROUP_DESTINATIONS = 25;
const MAX_ADDRESS_VALUES = 64;

/** Resolve all real locally hosted Group addresses without first-address bias. */
export async function selectVerifiedLocalGroupDestinations(
  origin: string,
  route: VerifiedInboxRoute,
  verifiedActivityJson: Record<string, unknown>,
  resolveLocalGroup: (candidate: string) => Promise<string | null>,
): Promise<LocalGroupIngressSelection> {
  const addresses = new Set<string>();
  const outer = asRecord(verifiedActivityJson.object);
  const nested = outer != null && ["Create", "Update", "Delete"].includes(
    typeof outer.type === "string" ? outer.type : "",
  ) ? asRecord(outer.object) : outer;
  const sources = [verifiedActivityJson, outer, nested];

  for (const source of sources) {
    if (source == null) continue;
    for (const field of ["to", "cc", "audience"]) {
      const values = source[field];
      if (values == null) continue;
      const input = Array.isArray(values) ? values : [values];
      // Cap both the input and distinct destination count before any DB reads.
      if (input.length > MAX_ADDRESS_VALUES) return { kind: "rejected" };
      for (const value of input) {
        const id = typeof value === "string" ? value :
          (typeof value === "object" && value !== null && "id" in value &&
            typeof value.id === "string" ? value.id : null);
        if (id == null) return { kind: "rejected" };
        addresses.add(id);
        if (addresses.size > MAX_ADDRESS_VALUES) return { kind: "rejected" };
      }
    }
  }

  let base: URL;
  try { base = new URL(origin); } catch { return { kind: "rejected" }; }
  const candidates: string[] = [];
  for (const id of addresses) {
    let url: URL;
    try { url = new URL(id); } catch { continue; }
    if (url.origin !== base.origin) continue;
    // Other public/bridge service actor recipients are not local Groups. Old
    // local community aliases are ambiguous: never silently route them.
    if (/^\/(?:c|communities)\//.test(url.pathname)) return { kind: "rejected" };
    if (!url.pathname.startsWith("/actors/communities/")) continue;
    if (!/^\/actors\/communities\/[^/]+$/.test(url.pathname) ||
        url.search || url.hash || url.href !== id) return { kind: "rejected" };
    candidates.push(id);
    if (candidates.length > MAX_GROUP_DESTINATIONS) return { kind: "rejected" };
  }
  if (candidates.length === 0) {
    // Actor-specific inbox delivery cannot fall back to some other tenant.
    return route.kind === "actor" && route.owner?.kind === "community"
      ? { kind: "rejected" } : { kind: "remote" };
  }
  if (route.kind === "unknown") return { kind: "rejected" };
  const groups: string[] = [];
  for (const candidate of candidates) {
    const canonical = await resolveLocalGroup(candidate);
    // A signed address alone does not prove that a public Group exists here.
    if (canonical !== candidate) return { kind: "rejected" };
    groups.push(canonical);
  }
  groups.sort();
  if (route.kind === "actor") {
    if (route.owner?.kind !== "community" ||
        !groups.includes(route.owner.actorUrl)) return { kind: "rejected" };
    // A specific inbox has exactly one server-owned destination even when the
    // same signed Create lists other local Groups in its recipient set.
    return { kind: "selected", groups: [route.owner.actorUrl] };
  }
  return { kind: "selected", groups };
}

function asRecord(value: unknown): Record<string, unknown> | null {
  // Only structured objects can contain another level of addressing.
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}
