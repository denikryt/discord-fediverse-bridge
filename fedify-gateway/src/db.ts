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

export interface LocalCommunityRow {
  // Local communities are Python-owned rows that define one Discord-backed
  // Group actor the gateway must expose and sign on behalf of.
  slug: string;
  actorUrl: string;
  inboxUrl: string;
  outboxUrl: string;
  followersUrl: string;
  displayName: string;
  summary: string;
  publicKeyPem: string;
  privateKeyPem: string;
}

export interface LocalCommunityFollowerRow {
  // Accepted follower rows define the concrete remote inbox fanout targets for
  // one Discord-backed local community publish.
  remoteActorId: string;
  remoteInboxUrl: string;
  followActivityId: string;
  status: string;
}

export interface PublishedActivityObjectRow {
  // The durable object row contains enough data to reconstruct a local Page or
  // Note without relying on the original publish process still being in memory.
  actorUsername: string;
  actorUrl: string;
  communityActorUrl: string;
  activityId: string;
  objectId: string;
  kind: "post" | "comment";
  title: string | null;
  bodyMarkdown: string;
  inReplyToObjectId: string | null;
  publishedAt: string;
  discordChannelId: number | null;
  discordMessageId: number | null;
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
    let statement;
    try {
      statement = database.prepare(`
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
    } catch (error) {
      if (isMissingUsersTableError(error)) {
        return null;
      }
      throw error;
    }
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

export async function loadLocalCommunityBySlug(
  config: GatewayConfig,
  slug: string,
): Promise<LocalCommunityRow | null> {
  const database = await openConfiguredDatabase(config);
  try {
    let statement;
    try {
      statement = database.prepare(`
        SELECT
          slug,
          actor_url,
          inbox_url,
          outbox_url,
          followers_url,
          display_name,
          summary,
          public_key_pem,
          private_key_pem
        FROM local_communities
        WHERE slug = ?
        LIMIT 1
      `);
    } catch (error) {
      if (isMissingLocalCommunitiesTableError(error)) {
        return null;
      }
      throw error;
    }
    try {
      statement.bind([slug]);
      if (!statement.step()) {
        return null;
      }
      const row = statement.getAsObject() as Record<string, unknown>;
      return {
        slug: asString(row.slug),
        actorUrl: asString(row.actor_url),
        inboxUrl: asString(row.inbox_url),
        outboxUrl: asString(row.outbox_url),
        followersUrl: asString(row.followers_url),
        displayName: asString(row.display_name),
        summary: asString(row.summary),
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

export async function loadLocalCommunityByActorUrl(
  config: GatewayConfig,
  actorUrl: string,
): Promise<LocalCommunityRow | null> {
  const database = await openConfiguredDatabase(config);
  try {
    let statement;
    try {
      statement = database.prepare(`
        SELECT
          slug,
          actor_url,
          inbox_url,
          outbox_url,
          followers_url,
          display_name,
          summary,
          public_key_pem,
          private_key_pem
        FROM local_communities
        WHERE actor_url = ?
        LIMIT 1
      `);
    } catch (error) {
      if (isMissingLocalCommunitiesTableError(error)) {
        return null;
      }
      throw error;
    }
    try {
      statement.bind([actorUrl]);
      if (!statement.step()) {
        return null;
      }
      const row = statement.getAsObject() as Record<string, unknown>;
      return {
        slug: asString(row.slug),
        actorUrl: asString(row.actor_url),
        inboxUrl: asString(row.inbox_url),
        outboxUrl: asString(row.outbox_url),
        followersUrl: asString(row.followers_url),
        displayName: asString(row.display_name),
        summary: asString(row.summary),
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

export async function loadAcceptedLocalCommunityFollowersByActorUrl(
  config: GatewayConfig,
  actorUrl: string,
): Promise<LocalCommunityFollowerRow[]> {
  const database = await openConfiguredDatabase(config);
  try {
    let statement;
    try {
      statement = database.prepare(`
        SELECT
          follower.remote_actor_id,
          follower.remote_inbox_url,
          follower.follow_activity_id,
          follower.status
        FROM local_community_followers AS follower
        JOIN local_communities AS community
          ON community.id = follower.local_community_id
        WHERE community.actor_url = ?
          AND follower.status = 'accepted'
        ORDER BY follower.created_at, follower.id
      `);
    } catch (error) {
      if (
        isMissingLocalCommunitiesTableError(error) ||
        isMissingLocalCommunityFollowersTableError(error)
      ) {
        return [];
      }
      throw error;
    }
    try {
      statement.bind([actorUrl]);
      const rows: LocalCommunityFollowerRow[] = [];
      while (statement.step()) {
        const row = statement.getAsObject() as Record<string, unknown>;
        rows.push({
          remoteActorId: asString(row.remote_actor_id),
          remoteInboxUrl: asString(row.remote_inbox_url),
          followActivityId: asString(row.follow_activity_id),
          status: asString(row.status),
        });
      }
      return rows;
    } finally {
      statement.free();
    }
  } finally {
    database.close();
  }
}

export async function loadPublishedActivityObjectByObjectId(
  config: GatewayConfig,
  objectId: string,
): Promise<PublishedActivityObjectRow | null> {
  // Object lookups always read the latest SQLite snapshot so a gateway restart
  // is not required before newly published objects become resolvable.
  return await loadPublishedActivityObjectByColumn(config, "object_id", objectId);
}

export async function loadPublishedActivityObjectByActivityId(
  config: GatewayConfig,
  activityId: string,
): Promise<PublishedActivityObjectRow | null> {
  // Activity lookups expose the durable embedded Create ids that remote servers
  // may dereference after accepting a community Announce(Create(Page|Note)).
  return await loadPublishedActivityObjectByColumn(config, "activity_id", activityId);
}

export async function loadPublishedActivityObjectByObjectIdForDatabaseUrl(
  databaseUrl: string,
  objectId: string,
): Promise<PublishedActivityObjectRow | null> {
  // Normalize.ts reads by exact object URL outside the main server bootstrap,
  // so this helper accepts a bare DATABASE_URL instead of a full config object.
  return await loadPublishedActivityObjectByColumnForDatabaseUrl(
    databaseUrl,
    "object_id",
    objectId,
  );
}

export async function loadPublishedActivityObjectByActivityIdForDatabaseUrl(
  databaseUrl: string,
  activityId: string,
): Promise<PublishedActivityObjectRow | null> {
  // Verification tests read Create activity rows directly by their durable
  // activity_id, matching the public dereference route behavior.
  return await loadPublishedActivityObjectByColumnForDatabaseUrl(
    databaseUrl,
    "activity_id",
    activityId,
  );
}

async function openConfiguredDatabase(
  config: GatewayConfig,
): Promise<Database> {
  return await openDatabaseUrl(config.databaseUrl);
}

async function openDatabaseUrl(databaseUrl: string): Promise<Database> {
  const filePath = resolveSqliteFilePath(databaseUrl);
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

function asNullableString(value: unknown): string | null {
  if (value == null) {
    return null;
  }
  return asString(value);
}

function asNullableNumber(value: unknown): number | null {
  if (value == null) {
    return null;
  }
  if (typeof value !== "number") {
    throw new Error("Expected a numeric column value from the shared database");
  }
  return value;
}

type PublishedActivityObjectLookupColumn = "object_id" | "activity_id";

async function loadPublishedActivityObjectByColumn(
  config: GatewayConfig,
  column: PublishedActivityObjectLookupColumn,
  value: string,
): Promise<PublishedActivityObjectRow | null> {
  let database: Database;
  try {
    database = await openConfiguredDatabase(config);
  } catch (error) {
    if (isMissingSqliteStorageError(error)) {
      return null;
    }
    throw error;
  }
  try {
    return loadPublishedActivityObjectFromDatabase(database, column, value);
  } finally {
    database.close();
  }
}

async function loadPublishedActivityObjectByColumnForDatabaseUrl(
  databaseUrl: string,
  column: PublishedActivityObjectLookupColumn,
  value: string,
): Promise<PublishedActivityObjectRow | null> {
  let database: Database;
  try {
    database = await openDatabaseUrl(databaseUrl);
  } catch (error) {
    if (isMissingSqliteStorageError(error)) {
      return null;
    }
    throw error;
  }
  try {
    return loadPublishedActivityObjectFromDatabase(database, column, value);
  } finally {
    database.close();
  }
}

function loadPublishedActivityObjectFromDatabase(
  database: Database,
  column: PublishedActivityObjectLookupColumn,
  value: string,
): PublishedActivityObjectRow | null {
  let statement;
  try {
    // object_id and activity_id identify the same durable local publish artifact.
    // Keeping both lookup paths on one query/mapper prevents Mastodon activity
    // dereference behavior from drifting away from object dereference behavior.
    statement = database.prepare(`
      SELECT
        actor_username,
        actor_url,
        community_actor_url,
        activity_id,
        object_id,
        kind,
        title,
        body_markdown,
        in_reply_to_object_id,
        published_at,
        discord_channel_id,
        discord_message_id
      FROM published_activity_objects
      WHERE ${column} = ?
      LIMIT 1
    `);
  } catch (error) {
    if (isMissingPublishedObjectsTableError(error)) {
      return null;
    }
    throw error;
  }
  try {
    statement.bind([value]);
    if (!statement.step()) {
      return null;
    }
    return mapPublishedActivityObjectRow(statement.getAsObject() as Record<string, unknown>);
  } finally {
    statement.free();
  }
}

function mapPublishedActivityObjectRow(
  row: Record<string, unknown>,
): PublishedActivityObjectRow {
  const kind = asString(row.kind);
  if (kind !== "post" && kind !== "comment") {
    throw new Error(`Unsupported published object kind: ${kind}`);
  }
  return {
    actorUsername: asString(row.actor_username),
    actorUrl: asString(row.actor_url),
    communityActorUrl: asString(row.community_actor_url),
    activityId: asString(row.activity_id),
    objectId: asString(row.object_id),
    kind,
    title: asNullableString(row.title),
    bodyMarkdown: asString(row.body_markdown),
    inReplyToObjectId: asNullableString(row.in_reply_to_object_id),
    publishedAt: asString(row.published_at),
    discordChannelId: asNullableNumber(row.discord_channel_id),
    discordMessageId: asNullableNumber(row.discord_message_id),
  };
}

function isMissingSqliteStorageError(error: unknown): boolean {
  return (
    error instanceof Error &&
    "code" in error &&
    error.code === "ENOENT"
  );
}

function isMissingPublishedObjectsTableError(error: unknown): boolean {
  return (
    error instanceof Error &&
    error.message.includes("no such table: published_activity_objects")
  );
}

function isMissingLocalCommunitiesTableError(error: unknown): boolean {
  return (
    error instanceof Error &&
    error.message.includes("no such table: local_communities")
  );
}

function isMissingLocalCommunityFollowersTableError(error: unknown): boolean {
  return (
    error instanceof Error &&
    error.message.includes("no such table: local_community_followers")
  );
}

function isMissingUsersTableError(error: unknown): boolean {
  return (
    error instanceof Error &&
    error.message.includes("no such table: users")
  );
}
