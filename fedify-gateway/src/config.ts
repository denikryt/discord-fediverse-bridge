import process from "node:process";

export interface GatewayConfig {
  // GatewayConfig is the runtime contract between the Node gateway process and
  // the deployment environment around it.
  actorIdentifier: string;
  actorName: string;
  actorSummary: string;
  // Optional direct injection is retained only for isolated compatibility tests; loadConfig never populates it.
  bridgePrivateKeyJwkJson?: string | null;
  bridgePublicKeyJwkJson?: string | null;
  databaseUrl: string;
  fedifyOrigin: string;
  port: number;
  pythonBridgeEventsUrl: string;
  pythonBridgeSharedSecret: string;
  logLevel: "info" | "debug";
}

export interface GatewayContextData extends GatewayConfig {
  // Raw request data is attached only for inbox requests where Announce payload
  // recovery may need the original JSON body.
  activitypubRawJson?: unknown;
  activitypubRawBodySha256?: string;
}

function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

function parsePort(value: string | undefined, fallback: number): number {
  if (!value) {
    return fallback;
  }
  const parsed = Number.parseInt(value, 10);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error(`Invalid port value: ${value}`);
  }
  return parsed;
}

export function loadConfig(): GatewayConfig {
  // Defaults keep local development ergonomic while still failing fast for the
  // secrets and origins that define federation identity.
  const logLevel = process.env.LOG_LEVEL === "debug" ? "debug" : "info";
  return {
    actorIdentifier: process.env.FEDIFY_ACTOR_IDENTIFIER ?? "bridge",
    actorName: process.env.FEDIFY_ACTOR_NAME ?? "Discord Lemmy Bridge Gateway",
    actorSummary:
      process.env.FEDIFY_ACTOR_SUMMARY ??
      "Receives ActivityPub activities from Lemmy and forwards normalized events to the Python bridge.",
    databaseUrl: process.env.DATABASE_URL ?? "sqlite:///../bridge.db",
    fedifyOrigin: requireEnv("FEDIFY_ORIGIN"),
    port: parsePort(process.env.FEDIFY_PORT, 3000),
    pythonBridgeEventsUrl:
      process.env.PYTHON_BRIDGE_EVENTS_URL ??
      "http://127.0.0.1:8080/internal/activitypub/events",
    pythonBridgeSharedSecret: requireEnv("FEDIFY_SHARED_SECRET"),
    logLevel,
  };
}
