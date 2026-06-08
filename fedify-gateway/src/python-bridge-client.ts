import type { GatewayConfig } from "./config.js";
import type { InternalBridgeEvent, LocalCommunityDiscoveryRecord } from "./types.js";

export interface BridgeActorKeyRow {
  actorUrl: string;
  keyId: string;
  keyFormat: "jwk" | "pem";
  algorithm: string;
  publicKeyData: string;
  privateKeyData: string;
}

export interface RegisteredUserRow {
  activitypubUsername: string;
  actorUrl: string;
  inboxUrl: string;
  outboxUrl: string;
  followersUrl: string;
  publicKeyPem: string;
  privateKeyPem: string;
}

export interface LocalCommunityRow {
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

export interface RemoteSubscriberRow {
  remoteActorId: string;
  remoteInboxUrl: string;
  followActivityId: string;
  status: string;
}

export interface MessageMappingRow {
  sourcePlatform: string;
  sourceId: string;
  activityId: string;
  objectId: string;
  actorUrl: string;
  communityActorUrl: string;
  discordChannelId: number | null;
  discordMessageId: number | null;
}

export interface PublishedActivityObjectRow {
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

export interface ChannelCommunitySubscriptionRow {
  communityActorUrl: string;
  followActivityId: string;
  status: string;
}

export interface PythonBridgeReadClient {
  loadMessageMappingByObjectId(objectId: string): Promise<MessageMappingRow | null>;
  loadPublishedActivityObjectByObjectId(objectId: string): Promise<PublishedActivityObjectRow | null>;
}

export class PythonBridgeClient implements PythonBridgeReadClient {
  readonly baseUrl: string;
  readonly sharedSecret: string;

  constructor(baseUrl: string, sharedSecret: string) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.sharedSecret = sharedSecret;
  }

  async loadBridgeActorKey(): Promise<BridgeActorKeyRow> {
    const row = await this.fetchJson<Record<string, unknown>>("/internal/fedify/actors/bridge/key");
    if (row == null) throw new Error("Python bridge did not return the bridge actor key");
    return {
      actorUrl: requiredString(row, "actor_url"),
      keyId: requiredString(row, "key_id"),
      keyFormat: requiredKeyFormat(row, "key_format"),
      algorithm: requiredString(row, "algorithm"),
      publicKeyData: requiredString(row, "public_key_data"),
      privateKeyData: requiredString(row, "private_key_data"),
    };
  }

  async loadRegisteredUserByUsername(username: string): Promise<RegisteredUserRow | null> {
    const row = await this.fetchJson<Record<string, unknown>>(
      `/internal/fedify/actors/users/${encodeURIComponent(username)}`,
      { notFound: true },
    );
    return row == null ? null : {
      activitypubUsername: requiredString(row, "activitypub_username"),
      actorUrl: requiredString(row, "actor_url"),
      inboxUrl: requiredString(row, "inbox_url"),
      outboxUrl: requiredString(row, "outbox_url"),
      followersUrl: requiredString(row, "followers_url"),
      publicKeyPem: requiredString(row, "public_key_pem"),
      privateKeyPem: requiredString(row, "private_key_pem"),
    };
  }

  async loadLocalCommunityBySlug(slug: string): Promise<LocalCommunityRow | null> {
    const row = await this.fetchJson<Record<string, unknown>>(
      `/internal/fedify/actors/communities/${encodeURIComponent(slug)}`,
      { notFound: true },
    );
    return row == null ? null : mapCommunity(row);
  }

  async loadLocalCommunityByActorUrl(actorUrl: string): Promise<LocalCommunityRow | null> {
    const row = await this.fetchJson<Record<string, unknown>>(
      "/internal/fedify/actors/communities/resolve",
      { method: "POST", body: { actor_url: actorUrl }, notFound: true },
    );
    return row == null ? null : mapCommunity(row);
  }

  async listLocalCommunities(fedifyOrigin: string): Promise<LocalCommunityDiscoveryRecord[]> {
    const payload = await this.fetchJson<{ items?: unknown }>("/internal/fedify/communities");
    if (payload == null) throw new Error("Python bridge did not return community discovery data");
    const hostname = new URL(fedifyOrigin).hostname;
    return requiredArray(payload, "items").map((value) => {
      const row = requiredRecord(value);
      const slug = requiredString(row, "slug");
      return {
        id: requiredNumber(row, "id"),
        slug,
        name: slug,
        title: requiredString(row, "display_name"),
        description: nullableString(row, "summary"),
        actor_id: requiredString(row, "actor_url"),
        alternate_actor_id: new URL(`/c/${slug}`, fedifyOrigin).href,
        handle: `!${slug}@${hostname}`,
      };
    });
  }

  async loadAcceptedRemoteSubscribersByActorUrl(actorUrl: string): Promise<RemoteSubscriberRow[]> {
    const payload = await this.fetchJson<{ items?: unknown }>(
      "/internal/fedify/communities/subscribers",
      { method: "POST", body: { actor_url: actorUrl }, notFound: true },
    );
    if (payload == null) return [];
    return requiredArray(payload, "items").map((value) => {
      const row = requiredRecord(value);
      return {
        remoteActorId: requiredString(row, "remote_actor_id"),
        remoteInboxUrl: requiredString(row, "remote_inbox_url"),
        followActivityId: requiredString(row, "follow_activity_id"),
        status: requiredString(row, "status"),
      };
    });
  }

  async loadPublishedActivityObjectByObjectId(objectId: string): Promise<PublishedActivityObjectRow | null> {
    return await this.resolvePublishedObject({ object_id: objectId });
  }

  async loadPublishedActivityObjectByActivityId(activityId: string): Promise<PublishedActivityObjectRow | null> {
    return await this.resolvePublishedObject({ activity_id: activityId });
  }

  async loadMessageMappingByObjectId(objectId: string): Promise<MessageMappingRow | null> {
    const row = await this.fetchJson<Record<string, unknown>>(
      "/internal/fedify/message-mappings/resolve",
      { method: "POST", body: { object_id: objectId }, notFound: true },
    );
    return row == null ? null : {
      sourcePlatform: requiredString(row, "source_platform"),
      sourceId: requiredString(row, "source_id"),
      activityId: requiredString(row, "activity_id"),
      objectId: requiredString(row, "object_id"),
      actorUrl: requiredString(row, "actor_url"),
      communityActorUrl: requiredString(row, "community_actor_url"),
      discordChannelId: nullableNumber(row, "discord_channel_id"),
      discordMessageId: nullableNumber(row, "discord_message_id"),
    };
  }

  async listChannelCommunitySubscriptions(): Promise<ChannelCommunitySubscriptionRow[]> {
    const payload = await this.fetchJson<{ items?: unknown }>(
      "/internal/fedify/channel-community-subscriptions",
    );
    if (payload == null) throw new Error("Python bridge did not return subscription data");
    return requiredArray(payload, "items").map((value) => {
      const row = requiredRecord(value);
      return {
        communityActorUrl: requiredString(row, "community_actor_url"),
        followActivityId: requiredString(row, "follow_activity_id"),
        status: requiredString(row, "status"),
      };
    });
  }

  async deliverEvent(event: InternalBridgeEvent): Promise<void> {
    const body = JSON.stringify(event);
    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}/internal/activitypub/events`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${this.sharedSecret}`,
          "Content-Type": "application/json",
          "X-Bridge-Delivery-Id": event.delivery_id,
        },
        body,
      });
    } catch (error) {
      throw new Error(`Python bridge fetch failed for ${event.delivery_id}: ${errorMessage(error)}`);
    }
    if (!response.ok) {
      throw new Error(`Python bridge rejected delivery ${event.delivery_id}: ${response.status} ${response.statusText}`);
    }
  }

  private async resolvePublishedObject(body: Record<string, string>): Promise<PublishedActivityObjectRow | null> {
    const row = await this.fetchJson<Record<string, unknown>>(
      "/internal/fedify/published-objects/resolve",
      { method: "POST", body, notFound: true },
    );
    return row == null ? null : {
      actorUsername: requiredString(row, "actor_username"),
      actorUrl: requiredString(row, "actor_url"),
      communityActorUrl: requiredString(row, "community_actor_url"),
      activityId: requiredString(row, "activity_id"),
      objectId: requiredString(row, "object_id"),
      kind: requiredKind(row, "kind"),
      title: nullableString(row, "title"),
      bodyMarkdown: requiredString(row, "body_markdown"),
      inReplyToObjectId: nullableString(row, "in_reply_to_object_id"),
      publishedAt: requiredString(row, "published_at"),
      discordChannelId: nullableNumber(row, "discord_channel_id"),
      discordMessageId: nullableNumber(row, "discord_message_id"),
    };
  }

  private async fetchJson<T>(
    path: string,
    options: { method?: "GET" | "POST"; body?: unknown; notFound?: boolean } = {},
  ): Promise<T | null> {
    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}${path}`, {
        method: options.method ?? "GET",
        headers: {
          Authorization: `Bearer ${this.sharedSecret}`,
          ...(options.body === undefined ? {} : { "Content-Type": "application/json" }),
        },
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
      });
    } catch (error) {
      throw new Error(`Python bridge request failed for ${path}: ${errorMessage(error)}`);
    }
    if (response.status === 404 && options.notFound) return null;
    if (!response.ok) {
      throw new Error(`Python bridge request failed for ${path}: ${response.status} ${response.statusText}`);
    }
    try {
      return await response.json() as T;
    } catch {
      throw new Error(`Python bridge returned malformed JSON for ${path}`);
    }
  }
}

const clients = new Map<string, PythonBridgeClient>();

export function getPythonBridgeClient(config: GatewayConfig): PythonBridgeClient {
  const key = `${config.pythonBridgeInternalUrl}\0${config.pythonBridgeSharedSecret}`;
  let client = clients.get(key);
  if (client == null) {
    client = new PythonBridgeClient(config.pythonBridgeInternalUrl, config.pythonBridgeSharedSecret);
    clients.set(key, client);
  }
  return client;
}

export async function loadBridgeActorKey(config: GatewayConfig): Promise<BridgeActorKeyRow> {
  return await getPythonBridgeClient(config).loadBridgeActorKey();
}
export async function loadRegisteredUserByUsername(config: GatewayConfig, username: string): Promise<RegisteredUserRow | null> {
  return await getPythonBridgeClient(config).loadRegisteredUserByUsername(username);
}
export async function loadLocalCommunityBySlug(config: GatewayConfig, slug: string): Promise<LocalCommunityRow | null> {
  return await getPythonBridgeClient(config).loadLocalCommunityBySlug(slug);
}
export async function loadLocalCommunityByActorUrl(config: GatewayConfig, actorUrl: string): Promise<LocalCommunityRow | null> {
  return await getPythonBridgeClient(config).loadLocalCommunityByActorUrl(actorUrl);
}
export async function listLocalCommunities(config: GatewayConfig): Promise<LocalCommunityDiscoveryRecord[]> {
  return await getPythonBridgeClient(config).listLocalCommunities(config.fedifyOrigin);
}
export async function loadAcceptedRemoteSubscribersByActorUrl(config: GatewayConfig, actorUrl: string): Promise<RemoteSubscriberRow[]> {
  return await getPythonBridgeClient(config).loadAcceptedRemoteSubscribersByActorUrl(actorUrl);
}
export async function loadPublishedActivityObjectByObjectId(config: GatewayConfig, objectId: string): Promise<PublishedActivityObjectRow | null> {
  return await getPythonBridgeClient(config).loadPublishedActivityObjectByObjectId(objectId);
}
export async function loadPublishedActivityObjectByActivityId(config: GatewayConfig, activityId: string): Promise<PublishedActivityObjectRow | null> {
  return await getPythonBridgeClient(config).loadPublishedActivityObjectByActivityId(activityId);
}

function mapCommunity(row: Record<string, unknown>): LocalCommunityRow {
  return {
    slug: requiredString(row, "slug"), actorUrl: requiredString(row, "actor_url"),
    inboxUrl: requiredString(row, "inbox_url"), outboxUrl: requiredString(row, "outbox_url"),
    followersUrl: requiredString(row, "followers_url"), displayName: requiredString(row, "display_name"),
    summary: nullableString(row, "summary") ?? "", publicKeyPem: requiredString(row, "public_key_pem"),
    privateKeyPem: requiredString(row, "private_key_pem"),
  };
}
function requiredRecord(value: unknown): Record<string, unknown> { if (value == null || typeof value !== "object" || Array.isArray(value)) throw new Error("Malformed Python bridge response"); return value as Record<string, unknown>; }
function requiredString(row: Record<string, unknown>, key: string): string { const value=row[key]; if(typeof value!=="string") throw new Error(`Malformed Python bridge response field: ${key}`); return value; }
function nullableString(row: Record<string, unknown>, key: string): string | null { const value=row[key]; if(value===null||value===undefined)return null; if(typeof value!=="string")throw new Error(`Malformed Python bridge response field: ${key}`); return value; }
function requiredNumber(row: Record<string, unknown>, key: string): number { const value=row[key]; if(typeof value!=="number")throw new Error(`Malformed Python bridge response field: ${key}`); return value; }
function nullableNumber(row: Record<string, unknown>, key: string): number | null { const value=row[key]; if(value===null||value===undefined)return null; if(typeof value!=="number")throw new Error(`Malformed Python bridge response field: ${key}`); return value; }
function requiredArray(row: Record<string, unknown>, key: string): unknown[] { const value=row[key]; if(!Array.isArray(value))throw new Error(`Malformed Python bridge response field: ${key}`); return value; }
function requiredKeyFormat(row: Record<string, unknown>, key: string): "jwk"|"pem" { const value=requiredString(row,key); if(value!=="jwk"&&value!=="pem")throw new Error(`Malformed Python bridge response field: ${key}`); return value; }
function requiredKind(row: Record<string, unknown>, key: string): "post"|"comment" { const value=requiredString(row,key); if(value!=="post"&&value!=="comment")throw new Error(`Malformed Python bridge response field: ${key}`); return value; }
function errorMessage(error: unknown): string { return error instanceof Error ? error.message : String(error); }
