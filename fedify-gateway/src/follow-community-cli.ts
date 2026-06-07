import process from "node:process";

// This CLI is a thin operator tool for issuing a one-off follow request to the
// running local gateway.
const BRIDGE_GATEWAY_URL = process.env.BRIDGE_GATEWAY_URL;
const SHARED_SECRET = process.env.FEDIFY_SHARED_SECRET;
const communityActorUrl = process.argv[2];

if (!communityActorUrl) {
  console.error("Usage: tsx follow-community-cli.ts <community_actor_url>");
  console.error("Example: tsx follow-community-cli.ts https://lemmy.ml/c/technology");
  process.exit(1);
}

if (!BRIDGE_GATEWAY_URL) {
  console.error("Missing required environment variable: BRIDGE_GATEWAY_URL");
  process.exit(1);
}

const response = await fetch(`${BRIDGE_GATEWAY_URL}/follow-community`, {
  method: "POST",
  headers: {
    ...(SHARED_SECRET
      ? { Authorization: `Bearer ${SHARED_SECRET}` }
      : {}),
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ communityActorUrl }),
});

const result = await response.json();

if (!response.ok) {
  console.error("Error:", result);
  process.exit(1);
}

console.log("Successfully sent follow request for:", communityActorUrl);
console.log("Response:", result);
