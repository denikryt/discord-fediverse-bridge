import process from "node:process";

// CLI tool for sending Undo(Follow) to communities that still have pending/accepted
// subscriptions in the bridge database. Reads subscriptions from the DB and lets
// the operator pick which ones to unfollow.
//
// Usage:
//   tsx src/unfollow-community-cli.ts                      # list all subscriptions
//   tsx src/unfollow-community-cli.ts <community_actor_url> <follow_activity_id>
//   tsx src/unfollow-community-cli.ts --all                 # unfollow all

const BRIDGE_GATEWAY_URL = process.env.BRIDGE_GATEWAY_URL;
const SHARED_SECRET = process.env.FEDIFY_SHARED_SECRET;
const DATABASE_URL = process.env.DATABASE_URL ?? "sqlite:///../bridge.db";

async function sendUnfollow(communityActorUrl: string, followActivityId: string): Promise<void> {
  if (!BRIDGE_GATEWAY_URL) {
    throw new Error("Missing required environment variable: BRIDGE_GATEWAY_URL");
  }
  console.log(`  → Sending Undo(Follow) for ${communityActorUrl}`);
  const response = await fetch(`${BRIDGE_GATEWAY_URL}/unfollow-community`, {
    method: "POST",
    headers: {
      ...(SHARED_SECRET ? { Authorization: `Bearer ${SHARED_SECRET}` } : {}),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ communityActorUrl, followActivityId }),
  });

  const result = await response.json();
  if (!response.ok) {
    console.error(`  ✗ Failed: ${JSON.stringify(result)}`);
  } else {
    console.log(`  ✓ Done`);
  }
}

async function listSubscriptions(): Promise<{ communityActorUrl: string; followActivityId: string; status: string }[]> {
  // Read directly from SQLite via python so we don't need a JS DB driver.
  const { execSync } = await import("node:child_process");
  const dbPath = DATABASE_URL.replace("sqlite:///", "");
  const output = execSync(
    `python3 -c "
import sqlite3, json
conn = sqlite3.connect('${dbPath}')
rows = conn.execute('SELECT lemmy_community_actor_id, follow_activity_id, status FROM channel_community_subscriptions WHERE follow_activity_id IS NOT NULL').fetchall()
print(json.dumps([{'communityActorUrl': r[0], 'followActivityId': r[1], 'status': r[2]} for r in rows]))
conn.close()
"`,
    { encoding: "utf8" },
  );
  return JSON.parse(output.trim());
}

const args = process.argv.slice(2);

if (args.length === 0 || args[0] === "--list") {
  const subs = await listSubscriptions();
  if (subs.length === 0) {
    console.log("No subscriptions with follow_activity_id found.");
  } else {
    console.log(`Found ${subs.length} subscription(s):\n`);
    for (const s of subs) {
      console.log(`  [${s.status}] ${s.communityActorUrl}`);
      console.log(`          follow_id: ${s.followActivityId}`);
    }
    console.log("\nUsage:");
    console.log("  tsx src/unfollow-community-cli.ts --all");
    console.log("  tsx src/unfollow-community-cli.ts <community_actor_url> <follow_activity_id>");
  }
  process.exit(0);
}

if (args[0] === "--all") {
  const subs = await listSubscriptions();
  if (subs.length === 0) {
    console.log("No subscriptions to unfollow.");
    process.exit(0);
  }
  console.log(`Unfollowing ${subs.length} subscription(s)...\n`);
  for (const s of subs) {
    await sendUnfollow(s.communityActorUrl, s.followActivityId);
  }
  process.exit(0);
}

if (args.length === 2) {
  await sendUnfollow(args[0], args[1]);
  process.exit(0);
}

console.error("Usage:");
console.error("  tsx src/unfollow-community-cli.ts                            # list subscriptions");
console.error("  tsx src/unfollow-community-cli.ts --all                      # unfollow all");
console.error("  tsx src/unfollow-community-cli.ts <community_url> <follow_id>");
process.exit(1);
