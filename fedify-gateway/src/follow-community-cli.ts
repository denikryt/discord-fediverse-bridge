import process from "node:process";

const GATEWAY_URL = process.env.GATEWAY_URL ?? "http://localhost:3000";
const communityActorUrl = process.argv[2];

if (!communityActorUrl) {
  console.error("Usage: tsx follow-community-cli.ts <community_actor_url>");
  console.error("Example: tsx follow-community-cli.ts https://lemmy.ml/c/technology");
  process.exit(1);
}

const response = await fetch(`${GATEWAY_URL}/follow-community`, {
  method: "POST",
  headers: {
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
