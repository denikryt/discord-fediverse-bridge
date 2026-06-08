/** Test-only bridge read API backed by a SQLite fixture database. */
import { createServer, type ServerResponse } from "node:http";
import { DatabaseSync, type SQLInputValue } from "node:sqlite";

export async function startPythonBridgeFixture(databasePath: string): Promise<string> {
  const server = createServer(async (request, response) => {
    let body = "";
    for await (const chunk of request) body += chunk;
    response.setHeader("Content-Type", "application/json");
    response.setHeader("Cache-Control", "no-store");
    response.setHeader("Connection", "close");
    if (!request.headers.authorization?.startsWith("Bearer ")) return unauthorized(response);
    const database = new DatabaseSync(databasePath, { readOnly: true });
    try { route(database, request.url ?? "", request.method ?? "GET", body, response); }
    catch (error) { response.statusCode = 500; response.end(JSON.stringify({ detail: error instanceof Error ? error.message : String(error) })); }
    finally { database.close(); }
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  server.unref();
  const address = server.address();
  if (address == null || typeof address === "string") throw new Error("missing fixture address");
  return `http://127.0.0.1:${address.port}`;
}

function route(db: DatabaseSync, path: string, method: string, body: string, res: ServerResponse): void {
  if (path === "/internal/activitypub/events" && method === "POST") return json(res, { status: "processed", outcome: "delivered", detail: "fixture" });
  if (path === "/internal/fedify/actors/bridge/key") return row(res, one(db, "SELECT actor_url, key_id, key_format, algorithm, public_key_data, private_key_data FROM bridge_actor_keys LIMIT 1"));
  const user = path.match(/^\/internal\/fedify\/actors\/users\/(.+)$/);
  if (user) return row(res, one(db, "SELECT activitypub_username, actor_url, inbox_url, outbox_url, followers_url, public_key_pem, private_key_pem FROM users WHERE activitypub_username = ?", decodeURIComponent(user[1])));
  const community = path.match(/^\/internal\/fedify\/actors\/communities\/([^/]+)$/);
  if (community && path !== "/internal/fedify/actors/communities/resolve") return row(res, one(db, "SELECT slug, actor_url, inbox_url, outbox_url, followers_url, display_name, summary, public_key_pem, private_key_pem FROM local_communities WHERE slug = ?", decodeURIComponent(community[1])));
  if (path === "/internal/fedify/actors/communities/resolve") { const value=JSON.parse(body) as {actor_url:string}; return row(res, one(db, "SELECT slug, actor_url, inbox_url, outbox_url, followers_url, display_name, summary, public_key_pem, private_key_pem FROM local_communities WHERE actor_url = ?", value.actor_url)); }
  if (path === "/internal/fedify/communities") return json(res, { items: all(db, "SELECT id, slug, display_name, summary, actor_url FROM local_communities ORDER BY LOWER(display_name), LOWER(slug), id") });
  if (path === "/internal/fedify/communities/subscribers") { const value=JSON.parse(body) as {actor_url:string}; const c=one(db,"SELECT id FROM local_communities WHERE actor_url = ?",value.actor_url); if(c==null)return missing(res); return json(res,{items:all(db,"SELECT remote_actor_id, remote_inbox_url, follow_activity_id, status FROM remote_subscribers WHERE local_community_id = ? AND status = 'accepted' ORDER BY created_at, id",Number(c.id))}); }
  if (path === "/internal/fedify/published-objects/resolve") { const value=JSON.parse(body) as {object_id?:string;activity_id?:string}; const column=value.object_id!=null?"object_id":"activity_id"; return row(res,one(db,`SELECT actor_username, actor_url, community_actor_url, activity_id, object_id, kind, title, body_markdown, in_reply_to_object_id, published_at, discord_channel_id, discord_message_id FROM published_activity_objects WHERE ${column} = ?`,value.object_id??value.activity_id)); }
  if (path === "/internal/fedify/message-mappings/resolve") { const value=JSON.parse(body) as {object_id:string}; return row(res,one(db,"SELECT source_platform, source_id, activity_id, object_id, actor_url, community_actor_url, discord_channel_id, discord_message_id FROM message_mappings WHERE object_id = ?",value.object_id)); }
  if (path === "/internal/fedify/channel-community-subscriptions") return json(res,{items:all(db,"SELECT lemmy_community_actor_id AS community_actor_url, follow_activity_id, status FROM channel_community_subscriptions WHERE follow_activity_id IS NOT NULL ORDER BY created_at, id")});
  missing(res);
}
function one(db:DatabaseSync,sql:string,...params:unknown[]):Record<string,unknown>|null { try{return db.prepare(sql).get(...params as SQLInputValue[]) as Record<string,unknown>|undefined??null}catch(error){if(String(error).includes("no such table"))return null;throw error} }
function all(db:DatabaseSync,sql:string,...params:unknown[]):Record<string,unknown>[] { try{return db.prepare(sql).all(...params as SQLInputValue[]) as Record<string,unknown>[] }catch(error){if(String(error).includes("no such table"))return [];throw error} }
function row(res:ServerResponse,value:Record<string,unknown>|null):void { if(value==null)return missing(res);json(res,value); }
function json(res:ServerResponse,value:unknown):void { res.end(JSON.stringify(value)); }
function missing(res:ServerResponse):void { res.statusCode=404;json(res,{detail:"not found"}); }
function unauthorized(res:ServerResponse):void { res.statusCode=401;json(res,{detail:"unauthorized"}); }
