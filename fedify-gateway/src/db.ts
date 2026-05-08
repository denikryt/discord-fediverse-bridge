import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import initSqlJs, { type Database, type SqlJsStatic } from "sql.js";

import type { GatewayConfig } from "./config.js";

export interface RegisteredUserRow {
  // The gateway only needs the identity and key fields required to publish a
  // correct local actor document and later sign user-authored activities.
  activitypubUsername: string;
  actorUrl: string;
  inboxUrl: string;
  outboxUrl: string;
  followersUrl: string;
  publicKeyPem: string;
  privateKeyPem: string;
}

let sqlJsPromise: Promise<SqlJsStatic> | null = null;

export async function loadRegisteredUserByUsername(
  config: GatewayConfig,
  username: string,
): Promise<RegisteredUserRow | null> {
  // Every lookup opens a fresh read-only snapshot of the SQLite file so newly
  // registered users appear to the gateway without requiring a process restart.
  const database = await openConfiguredDatabase(config);
  try {
    const statement = database.prepare(`
      SELECT
        activitypub_username,
        actor_url,
        inbox_url,
        outbox_url,
        followers_url,
        public_key_pem,
        private_key_pem
      FROM users
      WHERE activitypub_username = ?
      LIMIT 1
    `);
    try {
      statement.bind([username]);
      if (!statement.step()) {
        return null;
      }
      const row = statement.getAsObject() as Record<string, unknown>;
      return {
        activitypubUsername: asString(row.activitypub_username),
        actorUrl: asString(row.actor_url),
        inboxUrl: asString(row.inbox_url),
        outboxUrl: asString(row.outbox_url),
        followersUrl: asString(row.followers_url),
        publicKeyPem: asString(row.public_key_pem),
        privateKeyPem: asString(row.private_key_pem),
      };
    } finally {
      statement.free();
    }
  } finally {
    database.close();
  }
}

async function openConfiguredDatabase(
  config: GatewayConfig,
): Promise<Database> {
  const filePath = resolveSqliteFilePath(config.databaseUrl);
  const sqlJs = await getSqlJs();
  const bytes = await readFile(filePath);
  return new sqlJs.Database(bytes);
}

async function getSqlJs(): Promise<SqlJsStatic> {
  // sql.js needs an explicit wasm locator under tsx/ESM, so the loader is kept
  // in one place and reused by every DB read path.
  if (sqlJsPromise == null) {
    const moduleDir = path.dirname(fileURLToPath(import.meta.url));
    sqlJsPromise = initSqlJs({
      locateFile: (file: string) =>
        path.resolve(moduleDir, "../node_modules/sql.js/dist", file),
    });
  }
  return await sqlJsPromise;
}

function resolveSqliteFilePath(databaseUrl: string): string {
  // Stage 3 only supports the shared local SQLite file created by the Python
  // bridge. Rejecting other schemes early keeps the gateway behavior explicit.
  if (!databaseUrl.startsWith("sqlite:///")) {
    throw new Error(
      `Unsupported DATABASE_URL for fedify-gateway: ${databaseUrl}`,
    );
  }
  const rawPath = databaseUrl.slice("sqlite:///".length);
  return path.resolve(process.cwd(), rawPath);
}

function asString(value: unknown): string {
  if (typeof value !== "string") {
    throw new Error("Expected a string column value from the shared database");
  }
  return value;
}
