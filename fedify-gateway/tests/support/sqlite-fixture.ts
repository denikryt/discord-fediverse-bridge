/** Minimal sql.js-compatible fixture using Node's built-in SQLite. */
import { randomUUID } from "node:crypto";
import { readFileSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync, type SQLInputValue } from "node:sqlite";

export class FixtureDatabase {
  private readonly database = new DatabaseSync(":memory:");

  run(sql: string, params?: unknown[]): void {
    if (params == null || params.length === 0) {
      this.database.exec(sql);
      return;
    }
    this.database.prepare(sql).run(...params as SQLInputValue[]);
  }

  export(): Uint8Array {
    const path = join(tmpdir(), `bridge-fixture-${randomUUID()}.sqlite3`);
    this.database.exec(`VACUUM INTO '${path.replaceAll("'", "''")}'`);
    try { return new Uint8Array(readFileSync(path)); }
    finally { unlinkSync(path); }
  }

  close(): void { this.database.close(); }
}

export default async function initSqlJs(_options?: unknown): Promise<{ Database: typeof FixtureDatabase }> {
  return { Database: FixtureDatabase };
}


/** Seed the persisted bridge actor JWK pair used by gateway contract tests. */
export function seedBridgeActorJwk(
  database: FixtureDatabase,
  actorUrl: string,
  privateKeyData: string,
  publicKeyData: string,
): void {
  database.run(`
    CREATE TABLE IF NOT EXISTS bridge_actor_keys (
      actor_url TEXT NOT NULL,
      key_id TEXT NOT NULL,
      key_format TEXT NOT NULL,
      algorithm TEXT NOT NULL,
      public_key_data TEXT NOT NULL,
      private_key_data TEXT NOT NULL
    )
  `);
  database.run(
    `INSERT INTO bridge_actor_keys VALUES (?, ?, 'jwk', 'RSASSA-PKCS1-v1_5', ?, ?)`,
    [actorUrl, `${actorUrl}#main-key`, publicKeyData, privateKeyData],
  );
}
