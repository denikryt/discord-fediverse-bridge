/** Test-only bridge read API backed by a SQLite fixture database. */
import { createServer, type Server, type ServerResponse } from "node:http";
import { DatabaseSync, type SQLInputValue } from "node:sqlite";
import type { Socket } from "node:net";

const activeServers = new Set<Server>();
const activeSockets = new Map<Server, Set<Socket>>();

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
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const sockets = new Set<Socket>();
  activeSockets.set(server, sockets);
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.once("close", () => sockets.delete(socket));
  });
  activeServers.add(server);
  server.once("close", () => {
    activeServers.delete(server);
    activeSockets.delete(server);
  });
  server.unref();
  const address = server.address();
  if (address == null || typeof address === "string") throw new Error("missing fixture address");
  return `http://127.0.0.1:${address.port}`;
}

export async function closeAllPythonBridgeFixtures(): Promise<void> {
  const servers = [...activeServers];
  await Promise.all(servers.map(async (server) => {
    await new Promise<void>((resolve, reject) => {
      server.close((error) => error == null ? resolve() : reject(error));
      for (const socket of activeSockets.get(server) ?? []) socket.destroy();
      server.closeAllConnections?.();
    });
  }));
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

const FIXED_RSA_PRIVATE_KEY_PEM = `-----BEGIN PRIVATE KEY-----
MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQClkjB4wtvwqAmZ
h3mibtc/qiNuf6aqLybNCSL5D9hZ4k09/b/H/15z1lNd/jXA1D57e+TvTyMJ0pww
eTsxFiRIDToTQh1xbb0ks+n3wzDDBlYbX2fvDusdVuUCoiXcKI9oHAItHLY/WPss
j/CgT7TKsA0Io8D7MMAAp6McxRXExv/ADiRw6pqbLq5GmF6SGnUN66mma7WTRwcP
Pjbr7qMXcquAMjKqIaA70SO7gi9/0ltbT2D/RyNOg3Nxc42osAkc4A/SPTAdRIWZ
pCl+5t02ST37FeSX8V+Ikp2QlsiDvAdTfesPD5XdTJqZqhuyN4t9SAd2CX1taNwi
/KLbOl55AgMBAAECggEACKJbIf8KZG5E4uNeV9bHJZAoLRvdy3/uOnDEqL+XaReM
ruQHz2SNLsYBXrRSJgpDcGHVflfOhFV99OarjCujDHlZGxY+bhL6kzqJI9UjWrY+
wQpsg3pQDz1/749YYVbhpJyaTfMye1L29TvT4O5LJ6s+4MGWX5sLFjWnw0kxUYu4
IfCZMSLT19+UYaFpf6Xh25kUnEcAAgQfZY00mVezL9B3qHAplpqNtXiMKX0IEbs0
QwWDxxsbnHjUOTrjnxIgbaACMKLYSXwgGUDJqySnYZNy2WNNAPiBZpm4uwJQ1z0N
ET5TwTImOpFCt1PUZqK0cDzlXoX8ixYGqMt7aWBvkQKBgQDShvLpSDxjuOrt5H/p
6ezH/b/Ie6/hsRvSofs02I7ffBr2tCRsslOzKjC0QUlkFyp3hwJnln2XThjKYj7T
iHKIHi4CHgvRaviI5kbAEM0ajM4+wbCxvZRnyRjFdAvFAZ7l1/BK2JFQyetAx/Ba
UPfcUjykjBLb4f/I3OAukg5qEQKBgQDJVWdJv3uzikjASH5DAjSV7Zj+C14zvjRG
YbzBmM5GrBT1NruvFUKFia54aEIPUcBpq0mqU+zbqtdaUdEfcGYj7wRQYMEoSkcm
QR+QcUTzfiacn3ip3grOQsugO7NL02ULqbjKWuLMkGiC3tJCtRaq8xosGSfPomHz
QXTWRfGF6QKBgA1ai/vqHhKBPz2ZuddfhCpnWQvhdJWPQ2GH7sQ6XE2mtJsjcBAI
+7Aoo/A7F+mmYoY0ZR2m+Q5o4L+tnaiTRhiGOYre1wcQjvU4DhLOvgPKHKb0aD3N
9aTjp5OWucxkPuz7Vn2Y4RbLyAVS6VcBPceW28vgKq4R1sSp5fpuP0XRAoGBALvy
ENDEgvqwnYd6ZPuyxFotigOloxPUfEIznRxwxCcvHwVmScCArS+xvoBCe2CHpYI/
Vy482ECb9Bspg3nA2Gi1CKbsG4S8Cj1Iz+lsA7z2R58wM1kHobi4nBQBhzfCqHJB
xvKH826ZZCa/UTLaj8WX2RfPh92JrbyCn0oj0vp5AoGBAK5Zy7oNwg7c0dk1IRxF
HWw0P3/vYWL4DwM3Qe/EOIYYCZoqNTwZmRhtS5AayHR3SWGum+7QuHVY7+K/vvQ8
zL9GTgMcSY4TwDePj0jkmOAr6JDNlSkQvPnT7qfOBdTYOpd6yJbPnSQrkNqWNGUh
Z5QxuU5OpEO1eLsdBOkO36SU
-----END PRIVATE KEY-----`;
const FIXED_RSA_PUBLIC_KEY_PEM = `-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEApZIweMLb8KgJmYd5om7X
P6ojbn+mqi8mzQki+Q/YWeJNPf2/x/9ec9ZTXf41wNQ+e3vk708jCdKcMHk7MRYk
SA06E0IdcW29JLPp98MwwwZWG19n7w7rHVblAqIl3CiPaBwCLRy2P1j7LI/woE+0
yrANCKPA+zDAAKejHMUVxMb/wA4kcOqamy6uRphekhp1Deuppmu1k0cHDz426+6j
F3KrgDIyqiGgO9Eju4Ivf9JbW09g/0cjToNzcXONqLAJHOAP0j0wHUSFmaQpfubd
Nkk9+xXkl/FfiJKdkJbIg7wHU33rDw+V3UyamaobsjeLfUgHdgl9bWjcIvyi2zpe
eQIDAQAB
-----END PUBLIC KEY-----`;

export async function importFixedRsaKeyPair(): Promise<CryptoKeyPair> {
  const algorithm = { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" };
  const privateKey = await crypto.subtle.importKey("pkcs8", decodePem(FIXED_RSA_PRIVATE_KEY_PEM), algorithm, true, ["sign"]);
  const publicKey = await crypto.subtle.importKey("spki", decodePem(FIXED_RSA_PUBLIC_KEY_PEM), algorithm, true, ["verify"]);
  return { privateKey, publicKey };
}

function decodePem(pem: string): ArrayBuffer {
  const bytes = Buffer.from(pem.replace(/-----[^-]+-----/g, "").replace(/\s+/g, ""), "base64");
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}
