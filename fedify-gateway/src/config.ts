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

function buildPythonBridgeEventsUrl(): string {
  const host = process.env.BRIDGE_BIND_HOST ?? "127.0.0.1";
  const port = parsePort(process.env.BRIDGE_BIND_PORT, 8080);
  return `http://${host}:${port}/internal/activitypub/events`;
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
    fedifyOrigin:
      process.env.PUBLIC_BASE_URL ?? requireEnv("FEDIFY_ORIGIN"),
    port: parsePort(process.env.GATEWAY_BIND_PORT, 3000),
    pythonBridgeEventsUrl: process.env.BRIDGE_EVENTS_URL ?? buildPythonBridgeEventsUrl(),
    pythonBridgeSharedSecret: requireEnv("FEDIFY_SHARED_SECRET"),
    logLevel,
  };
}
