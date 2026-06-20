# 144 — Unified ActivityPub Actor Namespace: Stage 1 Identity Inventory

## Scope and repository baseline

Stage 1 is documentation-only. It changes no production source, no runtime route registration, no tests, no fixtures, no migrations, no deployment configuration, and no persisted data. The only intended output is this inventory, which later stages use to avoid a false-complete typed-namespace cutover.

Repository state inspected:

```text
branch: refactor/user_actor_profile_refactoring
commit: a60cb6f03702845f7706f81e8270f5ee6c3ff982
stage artifact: plans/144_activitypub_identity_inventory.md
```

Umbrella contract applied here: old local ActivityPub actor/object/activity URLs are not preserved as compatibility aliases after the migration. Stage 1 records current ownership and later-stage disposition; it does not implement typed URLs.

## Search evidence and discovery method

The inventory was built from direct file inspection plus repository-wide searches. The searches are recorded because later stages should rerun equivalent commands against their own starting tree before changing code.

```text
rg -n "(/actors/|/users/|/communities/|/c/|/activities/|#main-key)" src fedify-gateway tests docs deploy compose*.yaml dev notes
rg -n "actor_url|inbox_url|outbox_url|followers_url|publicKey|key_id|follow_activity_id|activity_id|object_id|community_actor_url|remote_actor_id|remote_inbox_url|shared_inbox" src fedify-gateway tests docs
rg -n "setActorDispatcher|setInboxListeners|mapHandle|mapAlias|getActorKeyPairs|publicKey|sharedInbox|webfinger|WebFinger" fedify-gateway/src fedify-gateway/tests
```

The first search returned hits in production Python, production TypeScript, gateway tests, Python behavior tests, support fixtures, docs, and nginx config. The file list in the approved plan was not exhaustive; additional relevant URL-bearing surfaces discovered by search include `src/personal_feed_publisher.py`, `src/dashboard.py`, `src/db/repositories/activitypub_objects.py`, `src/db/repositories/content_mutations.py`, `src/fediverse_identity.py`, `src/community_discovery.py`, `src/community_labels.py`, and multiple gateway verify tests. These were classified below by ownership instead of being treated as unrelated grep noise.

## Inventory classification contract

The following labels are used throughout this file.

| Classification | Meaning for later stages |
| --- | --- |
| `local actor identity` | Locally owned Service, Person, or Group actor URL and derived inbox/outbox/followers/public-key-owner URLs. |
| `local content identity` | Locally owned post/comment object IDs and content Create/Announce/Update/Delete IDs. |
| `local lifecycle activity identity` | Locally emitted Accept(Follow), service Follow/Undo(Follow), user profile Update, or content mutation activity IDs. |
| `remote relationship state` | Follower/subscriber/subscription/follow rows tied to a local actor relationship and potentially invalid after local actor IDs change. |
| `delivery state` | Retry/outbound/relay rows whose source activity ID, target snapshot, or idempotency behavior affects restart and duplicate handling. |
| `diagnostics` | Receipts/logs/admin state not required as a public identity contract after restart. |
| `product route state` | Discord, dashboard, discovery, and account settings surfaces that may display URLs but do not own ActivityPub identity. |
| `external remote actor data` | Remote actor, inbox, shared inbox, object, activity, follow, and community IDs. These must not be rewritten. |

Later-stage expectation labels: `migrate in Stage 4`, `purge/invalidate in Stage 4`, `leave untouched as remote identity`, `leave until Stage 5 hardening`, `leave until Stage 6 hardening`, `cleanup/docs guard in Stage 7`, and `not affected by ActivityPub namespace migration`.

## Current public baseline identity shapes

No executable baseline fixture was committed. Node dependencies are not installed in the inspected checkout, so Stage 1 records the stable identity fields from the current route and builder code rather than pasting full JSON-LD. This avoids adding old-route JSON fixtures that Stage 7 would later need to remove. The executable paths are listed so Stage 3/4 can convert them into behavior tests.

| Actor or document | Current request path or owner | Current stable identity shape | Classification | Later disposition |
| --- | --- | --- | --- | --- |
| Bridge service actor | Fedify `setActorDispatcher("/actors/{identifier}")`; `getBridgeActorIdentity()` in `fedify-gateway/src/actor-store.ts`; bridge key from `src/actor_key_service.py` | `id=/actors/{FEDIFY_ACTOR_IDENTIFIER}`; `inbox=/inbox`; `outbox=/actors/{identifier}/outbox`; `followers=/actors/{identifier}/followers`; key owner/key id use `/actors/{identifier}#main-key`. | `local actor identity` | `migrate in Stage 4` to `/actors/services/{identifier}` with actor-specific inbox while retaining shared `/inbox` support. |
| Registered user actor | Fedify dispatcher `/actors/{username}` plus Hono compatibility route `/users/:username`; registration writes in `src/registration_service.py` | Persisted row uses `/actors/{username}` and derived `/inbox`, `/outbox`, `/followers`; public `/users/:username` renders the same user document from the row. Some tests still seed `/users/{username}` rows. | `local actor identity` plus compatibility route | `migrate in Stage 4`; future typed root `/actors/users/{username}`. Old `/users/:username` public AP behavior unsupported in Stage 4/7 according to route disposition. |
| Local-community actor | Hono `/communities/:slug` and `/c/:slug`; Fedify actor-specific inbox only for `/communities/{identifier}/inbox`; creation in `src/local_communities/service.py` | Persisted row uses `/communities/{slug}` with derived inbox/outbox/followers. Public `/c/:slug` renders the community actor as an alias. | `local actor identity` plus compatibility/product-discovery route | `migrate in Stage 4` to `/actors/communities/{slug}`; old `/communities/:slug` AP route unsupported; `/c/:slug` requires product-vs-AP disposition in Stage 4/7. |
| User outbox/followers collections | `fedify-gateway/src/routes/public-activitypub.ts`; `src/internal_fedify_api.py`; `fedify-gateway/src/user-outbox.ts` | `/actors/:identifier/outbox` and `/actors/:identifier/followers` exist for users; `/users/:username/outbox` and `/users/:username/followers` are compatibility aliases. `validateCanonicalUserOutboxUrl()` expects `/actors/{username}/outbox`. | `local actor identity` collections and delivery visibility | `migrate in Stage 4`; Stage 5 later proves follower collection/fanout behavior under `/actors/users/{username}`. |
| Community outbox/followers collections | `fedify-gateway/src/routes/public-activitypub.ts` | `/communities/:slug/outbox` and `/communities/:slug/followers` return empty collections derived from the old community actor root. | `local actor identity` collections | `migrate in Stage 4`; Stage 6 later hardens community follow/fanout under typed root. |
| User post/comment object route | `fedify-gateway/src/routes/public-activitypub.ts`; `src/personal_feed_publisher.py`; `src/personal_feed_conversations.py`; `fedify-gateway/src/outbound/personal-feed-renderer.ts` | Current generated personal-feed IDs use `/users/{username}/post/{operation_id}`, `/users/{username}/comment/{operation_id}`, and `/users/{username}/activities/create/{post|comment}/{operation_id}`. Hono dereference supports `/users/:username/post/:objectId`, `/users/:username/comment/:objectId`, and create-activity routes. | `local content identity` | Existing durable IDs migrate/classify in Stage 4; future emissions harden in Stage 5. |
| Community post/comment object route | `fedify-gateway/src/routes/public-activitypub.ts`; local-community runtime and relay repositories | Current public route supports `/communities/:slug/post/:objectId` and `/communities/:slug/comment/:objectId`. Runtime rows store AP object/activity IDs from remote or local publication paths. | mixed `local content identity` and `external remote actor data` depending on origin | Stage 4 must classify durable local rows; Stage 6 hardens community emissions and relay/fanout. |
| User profile Update activity | `src/user_profiles.py`; `fedify-gateway/src/routes/internal-profile.ts` | Python builds root-level `/activities/user-profile-update/{user_id}/{profile_version}` and sends `publicKeyId={user.actor_url}#main-key`. | `local lifecycle activity identity` and `delivery state` | Stage 4 migrates/purges retained durable rows if present; Stage 5 hardens future profile Update emissions. |
| Service Follow/Undo activities | `fedify-gateway/src/outbound/community-follow.ts`; Python follow repositories | Gateway builds root-level `/activities/follow/{timestamp}/{random}` and `/activities/undo/{timestamp}/{random}` for bridge service actor outbound community follow/unfollow. | `local lifecycle activity identity` plus `remote relationship state` | Stage 4 purges/invalidates or regenerates old service follow state; Stage 6 hardens future service Follow/Undo IDs. |
| User Accept(Follow) activity | `fedify-gateway/src/outbound/follow-accept.ts` | Gateway builds `/actors/{username}/activities/accept/{hash}` from Follow ID, actor URL, and remote actor ID. | `local lifecycle activity identity` | Stage 4 classifies durable rows; Stage 5 hardens to canonical `/actors/users/{username}/activities/accept/follows/{activity_id}`. |
| Community Accept(Follow) activity | `fedify-gateway/src/outbound/follow-accept.ts` | Gateway builds `/communities/{slug}/activities/accept/{timestamp-random}` using community slug and local community signing key. | `local lifecycle activity identity` | Stage 4 classifies durable rows; Stage 6 hardens to canonical `/actors/communities/{slug}/activities/accept/follows/{activity_id}`. |

## Production URL constructors, parsers, and lookup owners

| File | Current responsibility and URL behavior | Classification | Later disposition |
| --- | --- | --- | --- |
| `src/actor_key_service.py` | Initializes bridge service actor key row. `actor_url = {origin}/actors/{fedify_actor_identifier}` and `key_id = {actor_url}#main-key`. Also resolves user/local-community private key material by ID. | `local actor identity`; secret-bearing key boundary | `migrate in Stage 4`. Preserve current secret minimization: Stage 1 records field names only. |
| `src/registration_service.py` | Creates registered user actor rows under `/actors/{username}` and derives inbox/outbox/followers. Comment explicitly says actor/key ownership must stay aligned. | `local actor identity` | `migrate in Stage 4` for registration writes. |
| `src/local_communities/service.py` | Creates local-community rows under `/communities/{slug}` and derives inbox/outbox/followers. | `local actor identity` | `migrate in Stage 4` for creation writes. |
| `src/internal_fedify_api.py` | Authenticated private DTO layer. Exposes bridge key, user actors, user followers/outbox, community actors, community subscribers, published object lookup, conversation target lookup, message mapping lookup, and subscription list. Returns stored public URL fields unchanged. | private route DTO for `local actor identity`, `local content identity`, `delivery state`, and secret-bearing key fields | Stage 4 updates DTO values through migrated rows and lookup rules; do not confuse private `/internal/fedify/actors/...` paths with public actor identity. |
| `src/fedify_gateway_client.py` | Python client to gateway internal endpoints for follow/unfollow, publish, render profile updates, accept follow, relay, and delivery. Carries local/remote URLs in request DTOs. | bridge adapter; can contain both local and remote identity | Stage 4/5/6 update request payloads as callers change; no Stage 1 edit. |
| `src/personal_feed_publisher.py` | Creates personal-feed root object/activity IDs under `/users/{username}/post/{operation_id}` and `/users/{username}/activities/create/post/{operation_id}` before persistence/fanout. | `local content identity` | Stage 4 migrates existing durable rows if required; Stage 5 hardens future emissions. |
| `src/personal_feed_conversations.py` | Creates personal-feed comment object/activity IDs under `/users/{username}/comment/{operation_id}` and `/users/{username}/activities/create/comment/{operation_id}`. Looks up root owner by `root.actor_url` through `users.get_user_by_actor_url()`. Stores parent/root object IDs. | `local content identity`; lookup by exact actor URL; delivery state for direct reply target inbox snapshots | Stage 4 updates lookups and durable IDs; Stage 5 hardens future comment emission/fanout. |
| `src/user_profiles.py` | Builds user profile Update activity IDs under `/activities/user-profile-update/{user_id}/{profile_version}` and uses `{user.actor_url}#main-key` as `publicKeyId`. | `local lifecycle activity identity`; delivery state | Stage 4 classifies retained durable rows; Stage 5 hardens future profile Update ID grammar. |
| `src/content_sync/persistence.py` | Persists bridge publication outcomes into message mappings and published object rows with activity/object/actor/community actor IDs supplied by runtime/gateway. | `local content identity` or remote source identity depending on path | Stage 4 must classify rows by local/remote origin and preserve Discord mappings. |
| `src/content_publish_service.py` | Orchestrates Discord-origin publication through gateway and persistence. Uses gateway-returned activity/object IDs. | `local content identity` and delivery handoff | Stage 4/5/6 caller contracts update; Stage 1 records as owner but does not edit. |
| `src/activitypub_handlers.py` | Receives normalized gateway events and routes to local-community runtime or user follower handlers. Uses `target_actor_id`, `target_actor_kind`, object IDs, and remote actor IDs. | inbound dispatch; mixed local/remote identity | Stage 4 updates typed actor lookup; Stage 5/6 harden Follow/Undo behavior. |
| `src/user_actor_followers.py` | Resolves target User Actor through `users.get_user_by_actor_url(target_actor_id)`, persists/refreshes `user_actor_followers`, records Follow IDs, and calls `accept_user_actor_follow()`. Undo also resolves by actor URL. | `remote relationship state`; exact local actor URL lookup | Stage 4 migration of actor rows and stale follower invalidation; Stage 5 workflow hardening. |
| `src/local_communities/followers.py` | Resolves local community through `get_local_community_by_actor_url(target_actor_id)`, persists/refreshes `remote_subscribers`, and calls `accept_local_community_follow()`. | `remote relationship state`; exact local actor URL lookup | Stage 4 migration of community rows and stale subscriber invalidation; Stage 6 hardening. |
| `src/local_communities/federation_fanout.py` | Uses `local_community.actor_url` as signing actor and `remote_subscribers` as fanout targets. Persists relay source/delivery rows containing source and relay activity/object IDs. | `delivery state`; `local lifecycle activity identity`; external remote actor data | Stage 4 classifies retained rows; Stage 6 hardens relay/fanout URLs. |
| `src/community_sync/runtime.py` | Persists inbound/outbound AP object/activity IDs to local community thread/message groups, inbound conversation rows, and published object rows; resolves update/delete continuity by stored object IDs. | mixed `local content identity`, `external remote actor data`, and `delivery state` | Stage 4 must not bulk-rewrite remote IDs; Stage 6 hardens local community behavior. |
| `src/db/repositories/activitypub_objects.py` | Writes and reads `PublishedActivityObject` rows. `list_user_outbox()` includes compatibility lookup using both `user.actor_url` and `user.actor_url.replace("/actors/", "/users/")`. | `local content identity`; compatibility lookup | Stage 4 migrates/normalizes local rows; Stage 7 removes obsolete compatibility. |
| `src/db/repositories/content_mutations.py` | Looks up `PublishedActivityObject` by object ID, derives mutation activity ID from `published.actor_url` origin with `/users/{actor_username}/activities/{update|delete}/...`, stores request/canonical JSON, and queues delivery rows. | `local lifecycle activity identity`; JSON-bearing delivery state | Stage 4 classifies/migrates durable local IDs and JSON payloads; Stage 5/6 harden future mutation ID grammar. |
| `src/db/repositories/users.py` | Owns registered user lookups by ID, Discord user ID, username, and actor URL. | `local actor identity` lookup | Stage 4 updates exact actor URL lookup/migration. |
| `src/db/repositories/local_communities.py` | Owns local-community lookups by slug, forum channel, ID, and actor URL. | `local actor identity` lookup | Stage 4 updates exact actor URL lookup/migration. |
| `src/dashboard.py` | Public dashboard/product display surface includes actor URL fields for users and local communities. It does not own ActivityPub routing. | `product route state` | Keep display consistent after Stage 4; no Stage 1 behavior change. |
| `src/community_discovery.py` | Parses direct actor URLs and Lemmy-style `/c/<slug>` aliases when resolving selected remote/local community input. | product/discovery state plus external remote actor boundary | Stage 4/7 must avoid treating every `/c/` as AP actor compatibility; remote URLs remain untouched. |
| `src/community_labels.py` | Extracts Lemmy-style `/c/<slug>` labels from remote community actor URLs for user-facing display. | `external remote actor data` and product labels | Leave remote semantics untouched. |
| `src/fediverse_identity.py` | Parses/normalizes fediverse handles and URLs for identity/discovery surfaces. | product/discovery state | Update only if typed local actor identity requires it in Stage 4; do not rewrite remote identities. |
| `fedify-gateway/src/outbound/gateway-url.ts` | Builds gateway URLs and current user actor URI under `/actors/{username}`. | `local actor identity` helper | Stage 2 should replace/augment with typed helper; Stage 4 cuts over callers. |
| `fedify-gateway/src/actor-store.ts` | Maps bridge actor from config, user/community actors from Python rows, resolves actor kind by trying bridge then user then community against one untyped identifier, and loads signing keys. | `local actor identity`; public-key owner/signing boundary | Stage 4 changes kind parsing and typed lookup. |
| `fedify-gateway/src/actors.ts` | Builds Service/Person/Group actor documents from identity DTOs. Accepts actor inbox and shared inbox separately. | actor document rendering | Stage 4 feeds typed identities; no Stage 1 edit. |
| `fedify-gateway/src/webfinger.ts` | Maps service, user, `!community`, and plain community aliases to stored actor URL. Adds `/c/{slug}` alias for communities. | public discovery | Stage 4 emits typed `self` links; Stage 7 cleans stale docs/tests. |
| `fedify-gateway/src/published-objects.ts` | Renders stored published object rows. `canonicalActorUrlForRecord()` rewrites stored user actor origin to `/actors/{actorUsername}` for user records. | `local content identity`; compatibility canonicalization | Stage 4 updates to typed actor root; Stage 7 removes old fallback. |
| `fedify-gateway/src/user-outbox.ts` | Validates Python-owned user outbox URL as `/actors/{username}/outbox`. | collection URL validation | Stage 4 changes expected path to typed user root. |
| `fedify-gateway/src/routes/inbox-logging.ts` | Captures compact/raw ActivityPub request data only for `POST /inbox`; `isInboxPost()` rejects actor-specific inbox paths. | inbound diagnostic/raw payload boundary | Stage 3 prepares typed raw capture; Stage 4 cuts over production listener/logging. |

## Public and internal routes

### Gateway route composition and publicness

`fedify-gateway/src/server.ts` registers routes in this order: debug access logging, health routes, public discovery routes, public ActivityPub Hono routes, internal gateway action routes, inbox logging middleware, then Fedify middleware. This matters because explicit Hono routes can answer old public AP paths before Fedify sees a request.

| Route | Method | Owner | Current identity shape | Public/private | Later disposition |
| --- | --- | --- | --- | --- | --- |
| `/healthz` | GET | `fedify-gateway/src/routes/health.ts` | none | public health | not affected. |
| `/.well-known/webfinger` | GET | `fedify-gateway/src/routes/public-discovery.ts`; `webfinger.ts` | Stored actor URLs: service `/actors/{identifier}`, users from rows, communities from rows plus `/c/{slug}` alias | public discovery | Stage 4 typed `self` links. |
| `/.well-known/discord-fediverse-bridge/communities` | GET | `public-discovery.ts`; Python `/internal/fedify/communities` | `actor_id` from community row; `alternate_actor_id=/c/{slug}` | public product/discovery | Stage 4/7 decide AP vs product exposure; not Stage 1. |
| `/users/:username` | GET | `public-activitypub.ts` | User actor compatibility route; renders row identity | public AP | Stage 4 makes unsupported as AP route; Stage 7 cleanup. |
| `/communities/:slug` | GET | `public-activitypub.ts` | Community actor canonical old route | public AP | Stage 4 makes unsupported. |
| `/c/:slug` | GET | `public-activitypub.ts` | Community actor alias; also appears in discovery/docs | public AP/product-discovery ambiguity | Stage 4 classifies; Stage 7 docs cleanup. |
| `/communities/:slug/outbox` | GET | `public-activitypub.ts` | Old community collection | public AP | Stage 4 typed collection route. |
| `/communities/:slug/followers` | GET | `public-activitypub.ts` | Old community collection | public AP | Stage 4 typed collection route. |
| `/communities/:slug/post/:objectId` | GET | `public-activitypub.ts` | Old community object route using full request URL lookup | public AP | Stage 4 typed object route/migration. |
| `/communities/:slug/comment/:objectId` | GET | `public-activitypub.ts` | Old community object route using full request URL lookup | public AP | Stage 4 typed object route/migration. |
| `/users/:username/outbox` | GET | `public-activitypub.ts` | User outbox compatibility | public AP | Stage 4 typed collection route; Stage 5 behavior. |
| `/users/:username/followers` | GET | `public-activitypub.ts` | User followers compatibility | public AP | Stage 4 typed collection route; Stage 5 behavior. |
| `/users/:username/activities/create/post/:activityId` | GET | `public-activitypub.ts` | Old user Create(post) activity dereference | public AP | Stage 4 typed activity route/migration. |
| `/users/:username/activities/create/comment/:activityId` | GET | `public-activitypub.ts` | Old user Create(comment) activity dereference | public AP | Stage 4 typed activity route/migration. |
| `/users/:username/post/:objectId` | GET | `public-activitypub.ts` | Old user post object dereference | public AP | Stage 4 typed object route/migration. |
| `/users/:username/comment/:objectId` | GET | `public-activitypub.ts` | Old user comment object dereference | public AP | Stage 4 typed object route/migration. |
| `/actors/:identifier/outbox` | GET | `public-activitypub.ts` | Untyped actor collection; user gets real outbox, non-user local actor gets empty collection | public AP | Stage 4 typed `/actors/{+identifier}/outbox`; Stage 6 community behavior. |
| `/actors/:identifier/followers` | GET | `public-activitypub.ts` | Untyped actor collection; user gets follower list, non-user local actor gets empty collection | public AP | Stage 4 typed `/actors/{+identifier}/followers`; Stage 5/6 behavior. |
| Fedify `setActorDispatcher("/actors/{identifier}")` | GET by middleware | `federation.ts` | Untyped local actor dispatcher for bridge/user/community | public AP | Stage 4 changes to `/actors/{+identifier}`. |
| Fedify `setInboxListeners("/communities/{identifier}/inbox", "/inbox")` | POST | `federation.ts` | Community actor-specific inbox plus shared inbox | public AP | Stage 4 changes to `/actors/{+identifier}/inbox` plus `/inbox`; Stage 3 prepares parsing/logging. |
| `/follow-community`, `/unfollow-community`, `/accept-user-actor-follow`, `/accept-local-community-follow` | POST | `routes/internal-follow.ts` | Internal gateway action routes carrying actor URLs and follow IDs | internal/gateway | Stage 4/5/6 update payload contracts. |
| `/publish`, `/publish-local-community`, `/update`, `/delete`, `/render-personal-feed` | POST | `routes/internal-publish.ts` | Internal publication/action routes carrying object/activity IDs | internal/gateway | Stage 4/5/6 update payload contracts. |
| `/render-user-actor-profile-update` | POST | `routes/internal-profile.ts` | Internal render route for profile Update JSON | internal/gateway | Stage 5 hardening after Stage 4 classification. |
| `/send-local-community-relay`, `/send-user-actor-deliveries` | POST | `routes/internal-delivery.ts` | Internal delivery routes carrying source IDs and target snapshots | internal/gateway | Stage 5/6 behavior hardening. |
| Python `/internal/activitypub/events` | POST | `src/http_api.py` | Gateway-to-Python normalized AP event intake | private | Stage 4 updates target actor identity semantics. |
| Python `/internal/fedify/*` | GET/POST | `src/internal_fedify_api.py` | Gateway read models and signing keys | private | Stage 4 updates returned persisted identity values. |

### Fedify actor/key ownership flow

| Flow | Current owner | Current behavior | Risk for migration |
| --- | --- | --- | --- |
| Actor dispatch | `federation.ts` -> `resolveLocalActorKind()` -> actor builders | Single untyped identifier. Bridge wins if identifier equals config; users/communities are tried by same final segment. | A user and community with the same identifier cannot be resolved by kind until Stage 4 parses typed tails. |
| Key pair dispatch | `setKeyPairsDispatcher()` -> `loadActorKeyPair()` | Loads bridge key by config identifier, user key by username, then community key. | Key ID, `publicKey.owner`, actor document ID, and signing actor can drift if rows migrate separately. |
| `ctx.getActorKeyPairs()` | actor document building | Uses current untyped dispatcher identifier. | Typed Stage 4 must pass `users/alice`, `communities/python`, or `services/bridge` consistently, not the final segment only. |
| `mapHandle()` | `hasLocalActor(config, username)` | Plain acct username maps to same untyped identifier if any local actor exists. | WebFinger ambiguity must be deliberately preserved or changed in Stage 4. |
| `mapAlias()` | service actor URL and user alias parsing | Service alias recognized at `/actors/{config.actorIdentifier}`; user alias parses `/actors/{username}` and `/users/{username}`-style resources through helper. | Stage 4 must update typed aliases and avoid old compatibility. |
| Raw inbox capture | `routes/inbox-logging.ts` | Only `POST /inbox` is captured and attached to Fedify context. | Undo/Announce raw recovery can fail on typed actor-specific inboxes unless Stage 3/4 extend capture. |

## Persistence inventory

The table/column list below is derived from `src/models.py` and repository inspection. Secret-bearing fields are listed only by name and trust boundary; no key material is present in this inventory.

| Table | URL-bearing or identity columns | Classification | Main readers/writers | Later-stage expectation |
| --- | --- | --- | --- | --- |
| `bridge_actor_keys` | `actor_url`, `key_id`, `public_key_data`, `private_key_data` | `local actor identity`; secret-bearing signing state | `ActorKeyService`, internal Fedify key API, gateway actor-store | `migrate in Stage 4`. Keep private key access limited to existing internal gateway signing boundary. |
| `users` | `actor_url`, `inbox_url`, `outbox_url`, `followers_url`, `public_key_pem`, `private_key_pem` | `local actor identity`; secret-bearing user signing state | `RegistrationService`, users repo, internal API, actor-store, profile/follower/fanout services | `migrate in Stage 4`; future writes typed. |
| `local_communities` | `actor_url`, `inbox_url`, `outbox_url`, `followers_url`, `public_key_pem`, `private_key_pem` | `local actor identity`; secret-bearing community signing state | local-community service/repo, internal API, actor-store, fanout/follow services | `migrate in Stage 4`; future writes typed. |
| `message_mappings` | `activity_id`, `object_id`, `actor_url`, `community_actor_url` | `local content identity` for bridge-owned publications; may also hold remote source IDs by context | content persistence, internal lookup API, Discord mapping flows | Stage 4 must preserve Discord mappings while regenerating/migrating local AP IDs where needed. |
| `published_activity_objects` | `actor_url`, `community_actor_url`, `activity_id`, `object_id`, `in_reply_to_object_id`, `conversation_root_object_id` | `local content identity`; can reference remote parent/root IDs | activitypub objects repo, gateway dereference, outbox, content mutation repo, runtimes | `migrate in Stage 4` for locally owned IDs; leave remote parent IDs untouched. |
| `personal_feed_thread_mappings` | references `root_published_object_id`; Discord IDs only | local application binding | personal-feed runtime | Preserve; AP identity migration follows referenced published object rows. |
| `inbound_conversation_objects` | `object_id`, `activity_id`, `conversation_root_object_id`, `parent_object_id`, `actor_id`, `source_activity_json` | mostly `external remote actor data`; JSON may include local target IDs | personal-feed/community inbound runtimes | Leave remote IDs untouched; Stage 4 only inspects JSON if local owned IDs are embedded. |
| `inbound_discord_deliveries` | references inbound object rows and Discord target IDs | delivery state | inbound delivery runtime | Not directly migrated unless linked JSON/source references require classification. |
| `user_actor_followers` | `remote_actor_id`, `remote_inbox_url`, `remote_shared_inbox_url`, `follow_activity_id`; local actor via `user_id` | `remote relationship state` plus remote endpoint snapshots | user follower service, internal API, fanout snapshot logic | `purge/invalidate in Stage 4` if tied to old local actor identity; Stage 5 proves fresh typed follow. Remote fields are not rewritten. |
| `user_actor_follow_activity_ids` | `follow_activity_id`, `remote_actor_id`, local `user_id` | `remote relationship state` / idempotency | user follower service | `purge/invalidate in Stage 4` with old follower state; remote IDs are not rewritten. |
| `user_actor_outbound_sources` | `activity_id`, `published_object_id`, `snapshot_json`, `activity_json` | `delivery state`; `local lifecycle/content identity` in scalar and JSON | user actor delivery repo/worker, profile and personal-feed delivery | Stage 4 must classify JSON for locally owned IDs; Stage 5 hardens future profile/content source IDs. |
| `user_actor_outbound_deliveries` | `target_endpoint_url` | delivery state; remote endpoint snapshot | user actor delivery worker | Remote endpoint not rewritten; stale deliveries tied to old source IDs may be purged/invalidated in Stage 4. |
| `user_actor_outbound_delivery_followers` | `remote_actor_id`, `snapshotted_endpoint_url` | delivery snapshot of external remote identity | user actor delivery repo | Leave remote actor/endpoint values untouched; purge only if parent delivery/source invalidated. |
| `user_actor_content_mutations` | `activity_id`, `request_json`, `canonical_activity_json`, references `published_object_id` | `local lifecycle activity identity`; JSON-bearing delivery/retry state | content mutation repo, gateway mutation routes | Stage 4 must migrate/purge retained local IDs and JSON; future mutation grammar hardening follows Stage 5/6. |
| `channel_community_subscriptions` | `lemmy_community_actor_id`, `community_inbox_url`, `follow_activity_id` | remote subscription to external Lemmy community | subscribe/unsubscribe operations, internal API | `leave untouched as remote identity`; service actor follow state may be regenerated separately if local service actor ID is embedded in follow IDs. |
| `bridge_actor_follows` | `community_actor_id`, `follow_activity_id`, `community_inbox_url` | `remote relationship state` for bridge service actor follows external communities | remote subscription repo, gateway follow/unfollow | `purge/invalidate in Stage 4` or regenerate under typed service actor if required. Remote community actor/inbox untouched. |
| `remote_actors` | `actor_url`, `inbox_url`, `shared_inbox_url`, `public_key_pem` | `external remote actor data` | remote actor cache/follower/fanout code | `leave untouched as remote identity`. |
| `remote_subscribers` | `remote_actor_id`, `remote_inbox_url`, `follow_activity_id`, local `local_community_id` | `remote relationship state` for local communities | local-community follower service, fanout | `purge/invalidate in Stage 4` for old local actor relationships; Stage 6 proves fresh typed follow. Remote fields untouched. |
| `local_community_relay_source_activities` | `source_object_ap_id`, `source_activity_id`, `source_announce_id`, `origin_remote_actor_id`, `source_activity_json` | `delivery state`; mostly remote source identity; may contain local relay targets in JSON | local-community fanout repo | Stage 4 inspect JSON/scalars before migrating; do not rewrite remote source IDs. Stage 6 hardens relay behavior. |
| `local_community_relay_deliveries` | `source_object_ap_id`, `source_activity_id`, `origin_remote_actor_id`, `target_remote_actor_id`, `target_inbox_url`, `relay_activity_id` | delivery state; `relay_activity_id` may be locally emitted; remote IDs/endpoints external | local-community fanout repo | Stage 4 classifies local relay IDs; Stage 6 hardens future relay IDs. Remote values untouched. |
| `local_community_threads` | `ap_activity_id`, `ap_object_id` | local or remote content identity depending on direction/origin | local-community runtime/repo | Stage 4 classify by direction/origin and preserve Discord surface mapping. |
| `local_community_messages` | `ap_activity_id`, `ap_object_id`, `parent_ap_object_id` | local or remote content identity depending on direction/origin | local-community runtime/repo | Stage 4 classify by direction/origin; parent remote IDs untouched. |
| `community_thread_groups` | `community_actor_id`, `ap_activity_id`, `ap_object_id` | legacy/local application mapping with AP IDs | community sync runtime | Stage 4 preserve Discord mapping; migrate local AP IDs only. |
| `community_message_groups` | `community_actor_id`, `ap_activity_id`, `ap_object_id`, `parent_message_group_id` | legacy/local application mapping with AP IDs | community sync runtime | Stage 4 preserve Discord mapping; migrate local AP IDs only. |
| `post_links` | `lemmy_post_ap_id` | external Lemmy object identity | legacy mappings | `leave untouched as remote identity`. |
| `comment_links` | `lemmy_comment_ap_id`, `lemmy_parent_comment_ap_id` | external Lemmy object identity | legacy mappings | `leave untouched as remote identity`. |
| `activitypub_event_receipts` | `delivery_id`, `object_ap_id` | diagnostics/idempotency receipt for inbound events | HTTP event intake/outcome repo | Usually diagnostics; Stage 4 should not rewrite remote inbound IDs blindly. |
| `community_actor_bans` | `actor_url` | remote/local actor policy subject depending on scope | moderation repositories | Stage 4 classify local-vs-remote before any change; remote policy subjects untouched. |
| `local_subscribers`, `local_community_thread_surfaces`, `local_community_message_surfaces`, `personal_feed_bindings`, profile media/user profile field tables | no direct AP URL columns except references to owner rows | local application state | Discord/product runtimes | Preserve; not direct migration targets. |

JSON-bearing columns requiring later inspection instead of scalar string replacement: `user_actor_outbound_sources.snapshot_json`, `user_actor_outbound_sources.activity_json`, `user_actor_content_mutations.request_json`, `user_actor_content_mutations.canonical_activity_json`, `inbound_conversation_objects.source_activity_json`, `local_community_relay_source_activities.source_activity_json`, and audit `before_json`/`after_json` if those payloads store actor URLs for diagnostics. Stage 4 must not rewrite remote IDs inside JSON payloads by substring.

## Outbound and inbound activity ID families

| Family | Current grammar and owner | Durable storage | Dereference route | Classification | Target stage |
| --- | --- | --- | --- | --- | --- |
| User root post Create | `/users/{username}/activities/create/post/{operation_id}` from `src/personal_feed_publisher.py`; gateway renderer validates the same grammar | `published_activity_objects.activity_id`, `message_mappings.activity_id`, delivery source JSON | `GET /users/:username/activities/create/post/:activityId` | `local content identity` | Stage 4 migrate/classify existing; Stage 5 harden future. |
| User root post object | `/users/{username}/post/{operation_id}` | `published_activity_objects.object_id`, `message_mappings.object_id`, thread mappings by reference | `GET /users/:username/post/:objectId` | `local content identity` | Stage 4 then Stage 5. |
| User comment Create | `/users/{username}/activities/create/comment/{operation_id}` from `src/personal_feed_conversations.py` | published objects, mappings, delivery source JSON | `GET /users/:username/activities/create/comment/:activityId` | `local content identity` | Stage 4 then Stage 5. |
| User comment object | `/users/{username}/comment/{operation_id}` | published objects, mappings, replies/root columns | `GET /users/:username/comment/:objectId` | `local content identity` | Stage 4 then Stage 5. |
| User content Update/Delete | `/users/{username}/activities/update/{kind}/{operation_id}` and `/users/{username}/activities/delete/{operation_id}` from `content_mutations.py` / gateway outbound mutation builder | `user_actor_content_mutations.activity_id`, request/canonical JSON, delivery state | No explicit public GET route found for update/delete activities | `local lifecycle activity identity` | Stage 4 classify retained rows; Stage 5/6 harden. |
| User profile Update | `/activities/user-profile-update/{user_id}/{profile_version}` from `src/user_profiles.py` | user outbound source rows/JSON when delivery is queued | No standalone GET route found | `local lifecycle activity identity` | Stage 4 classify; Stage 5 harden. |
| User Accept(Follow) | `/actors/{username}/activities/accept/{hash}` from `fedify-gateway/src/outbound/follow-accept.ts` | May be externally observable in delivered JSON; durable follower row stores remote Follow ID, not Accept ID | No standalone GET route found | `local lifecycle activity identity` | Stage 5 future hardening after Stage 4 classification. |
| Community Accept(Follow) | `/communities/{slug}/activities/accept/{timestamp-random}` from `follow-accept.ts` | Delivered JSON; remote subscriber row stores remote Follow ID, not Accept ID | No standalone GET route found | `local lifecycle activity identity` | Stage 6 hardening. |
| Bridge service Follow | `/activities/follow/{timestamp}/{random}` from `community-follow.ts` | `bridge_actor_follows.follow_activity_id`, `channel_community_subscriptions.follow_activity_id` | No standalone GET route found | `local lifecycle activity identity`; remote relationship state | Stage 4 purge/regenerate; Stage 6 harden. |
| Bridge service Undo(Follow) | `/activities/undo/{timestamp}/{random}` from `community-follow.ts` | Delivered JSON; old follow activity ID references persisted | No standalone GET route found | `local lifecycle activity identity` | Stage 6 harden. |
| Community Announce relay/fanout | `/communities/{slug}/activities/announce/{timestamp-random}` from `local-community-fanout.ts` | `local_community_relay_deliveries.relay_activity_id`, delivery outcomes | No explicit GET route found | `local lifecycle activity identity` | Stage 4 classify existing; Stage 6 harden. |
| Remote inbound Create/Announce/Update/Delete | Remote URLs from remote actor/server payloads | inbound conversation, relay source, legacy link tables | Maybe remote dereference only | `external remote actor data` | Leave untouched; never rewrite by local migration. |

## Tests and fixtures inventory

No tests are changed in Stage 1. The following groups are relevant because stale hard-coded URLs can make later stages pass while bypassing real public discovery.

| Group | Examples found by search | Current role | Later disposition |
| --- | --- | --- | --- |
| Gateway actor/document route tests | `fedify-gateway/tests/verify-user-canonical-actor.ts`, `verify-local-community-canonical-key.ts`, `verify-user-outbox.ts`, `verify-user-actor-followers-collection.ts` | Assert current `/actors/{username}`, `/users/{username}`, `/communities/{slug}`, and key-owner behavior. | Stage 4 updates public-route expectations; Stage 7 removes old-compat tests. |
| Gateway outbound ID tests | `verify-accept-follow.ts`, `verify-follow-activity-id.ts`, `verify-unfollow-activity.ts`, `verify-content-mutation-identity.ts`, `verify-personal-feed-render.ts`, `verify-discord-to-lemmy-publish.ts` | Assert old Accept, Follow/Undo, content mutation, personal-feed, and community publication URL families. | Stage 5/6 harden future emissions; Stage 4 migrates durable state. |
| Gateway public object/store tests | `verify-published-object-store.ts`, `verify-python-bridge-read-client.ts`, `verify-normalize-*`, `verify-local-community-*` | Mix local old URL fixtures and remote actor URLs. Some tests bypass WebFinger/actor documents by direct fixture rows. | Later stages must distinguish local fixture rows from remote examples. |
| Python registration/user identity tests | `tests/test_registration_flow.py`, `tests/test_user_identity_dump.py`, `tests/test_internal_fedify_api.py`, `tests/test_bridge_actor_key_bootstrap.py` | Seed and assert user actor rows, bridge actor key rows, internal API DTOs. Some rows use `/users/{username}` as legacy data. | Stage 4 migration tests keep legacy fixtures intentionally; production writes become typed. |
| User Actor follow/fanout tests | `tests/behavior/test_user_actor_follow_scenarios.py`, `test_user_actor_content_fanout_scenarios.py`, `test_user_actor_profile_fanout_scenarios.py`, `test_user_actor_outbox_scenarios.py` | Prove Python persistence/fanout after normalized events but do not currently prove remote public HTTP delivery to advertised typed inbox. | Stage 5 end-to-end workflow tests through public gateway. |
| Local-community tests | `tests/behavior/test_local_community_*`, `tests/model_based/test_local_community_*`, `tests/support/local_community_relay.py` | Heavy use of `/communities/{slug}`, `/c/{slug}`, remote subscriber rows, relay/outbound IDs. | Stage 6 hardens typed community behavior; Stage 7 cleans old assumptions. |
| Remote URL tests that must not be rewritten | `tests/support/activitypub.py`, normalization tests with `https://lemmy.example/c/...`, Mastodon-style `/users/...` on remote hosts | Represent external remote actor/object/activity identities. | Leave untouched; assert local-only migration boundaries. |
| Deployment tests | `tests/test_docker_deployment.py` | Assert nginx/compose route exposure and internal route blocking. | Stage 4/7 update public AP route exposure assertions. |
| Docs/compatibility tests | docs-related checks if any; `docs/discord_lemmy_bridge_compatibility_catalog.md` references | Some docs conflict with umbrella's no-compatibility decision. | Stage 7 documentation cleanup; Stage 1 only records. |

## Deployment and documentation surfaces

| Surface | Current observed behavior or text | Classification | Later disposition |
| --- | --- | --- | --- |
| `deploy/nginx/default.conf.template` | Publicly proxies `/.well-known/webfinger`, `/.well-known/discord-fediverse-bridge/`, `/nodeinfo/`, `/actors/`, `/c/`, `/users/`, `/communities/`, `/inbox`, and `/activitypub/` to the gateway; blocks `/internal/`. | deployment route exposure | Stage 4 updates supported public AP route classification; Stage 7 removes old AP exposure if required. |
| `compose.yaml`, `compose.nginx.yaml`, `compose.build.yaml` | Launch Python/gateway/nginx paths; no direct actor grammar found in Compose search beyond environment/origin wiring. | deployment path | Ensure Stage 4 works under all launch paths; Stage 1 no edit. |
| `fedify-gateway/SETUP.md`, `fedify-gateway/nginx.conf`, `fedify-gateway/nginx-setup.sh` | Gateway setup/proxy docs may expose old `/actors`, `/users`, `/communities`, `/c`, `/inbox` examples. | docs/deployment | Stage 7 cleanup if covered by docs responsibility. |
| `docs/FEDERATION.md` | Purpose is federation profile. Currently documents bridge actor `/actors/bridge`, user `/users/{username}`, local community `/communities/{slug}` and `/c/{slug}`, user followers alias behavior, and old object/activity descriptions. | protocol docs | Stage 7 must update after behavior changes. |
| `docs/architecture/http-routes.md` | Purpose is route/process/publicness ownership. Currently lists `/users`, `/communities`, `/c`, `/inbox`, and internal gateway action routes. | route docs | Stage 4/7 update route map. |
| `docs/architecture/database-map.md` | Purpose covers schema/data ownership. Needs update when Stage 4 migration policy is implemented. | schema docs | Stage 4/7 update if changed. |
| `docs/architecture/event-flows.md` | Purpose covers event/data flow. It references AP identity and delivery/fanout paths. | flow docs | Stage 5/6/7 update with final typed behavior. |
| `docs/development/navigation.md` | Purpose is developer navigation. References local community actor rendering, remote AP flow, and key files. | navigation docs | Stage 7 update after cleanup. |
| `docs/discord_lemmy_bridge_compatibility_catalog.md` | Purpose is existing compatibility catalog. It currently says ActivityPub URL aliases and payload fallbacks should be kept long-term, including `/users/{username}` compatibility. This conflicts with the approved umbrella's no-compatibility decision. | stale compatibility docs | Stage 7 must rewrite/remove old compatibility claims; Stage 1 treats umbrella as authority. |
| `dev/future_tasks.md` | Contains private-key isolation future task and must not be expanded with completion notes. | future work docs | No Stage 1 edit. Key isolation reminder supports not broadening secret consumers. |
| `notes/known_issues.md` | Current issues do not directly track the typed-namespace migration. | known issues journal | No Stage 1 edit unless a new limitation is discovered during implementation; none was. |

## Security and secret boundaries

Stage 1 did not copy private key material, bearer tokens, Discord tokens, OAuth secrets, raw ActivityPub payloads, or internal Authorization headers into the inventory. Secret-bearing fields are listed only by table/DTO name.

Current secret-bearing boundaries to preserve in later stages:

| Secret-bearing data | Current owner and consumer | Stage 1 finding |
| --- | --- | --- |
| `bridge_actor_keys.private_key_data` | Python persistence; authenticated internal Fedify API; gateway actor-store loads for bridge service signing. | Later key ID/owner migration must not add new consumers. |
| `users.private_key_pem` | Python persistence; authenticated internal Fedify API; gateway actor-store loads for User Actor signing. | Stage 4 changes actor URLs/key IDs, not trust boundary. |
| `local_communities.private_key_pem` | Python persistence; authenticated internal Fedify API; gateway local-community key helpers load for Group actor signing. | Stage 4 changes actor URLs/key IDs, not trust boundary. |
| `FEDIFY_SHARED_SECRET` / internal bearer | Runtime configuration for Python<->gateway internal API authorization. | Inventory records boundary only; no secret value copied. |

The approved future task to isolate User Actor private keys remains outside this umbrella. New typed URL helper or migration code must accept non-secret actor identifiers and origins where possible and load private keys only at the gateway signing boundary.

## Stage handoff notes

### Stage 2 can rely on this helper coverage list

The Python and TypeScript identity builders must cover at least:

- service, user, and community actor root URLs;
- actor-specific inbox, outbox, followers, and `#main-key` public-key-owner URLs;
- user post/comment object IDs;
- user Create post/comment activity IDs;
- user Update/Delete content activity IDs;
- user Accept(Follow) activity IDs;
- user profile Update activity IDs;
- community post/comment object IDs;
- community Create/Announce/Update/Delete activity IDs;
- community Accept(Follow) activity IDs;
- bridge service Follow and Undo(Follow) activity IDs;
- parsing/distinguishing `/actors/users/{identifier}`, `/actors/communities/{identifier}`, and `/actors/services/{identifier}` without final-segment collision.

### Stage 3 can rely on these gateway preparation gaps

- Production Fedify currently uses `/actors/{identifier}` and `/communities/{identifier}/inbox`; no typed `{+identifier}` production route exists.
- Raw inbox capture only recognizes `POST /inbox`; typed actor-specific inbox paths are not captured.
- Current workflow tests can use internal fakes/fixtures that bypass remote actor dereference and public route discovery; Stage 3 should prepare outer-boundary fake remote actor dereference and typed route fixtures without changing production routing.
- `resolveLocalActorKind()` tries bridge/user/community for one untyped identifier and does not parse actor kind.

### Stage 4 must own these atomic cutover surfaces

- Fedify actor dispatcher, inbox listener, keypair dispatcher, `mapHandle`, `mapAlias`, and `ctx.getActorKeyPairs()` identifier semantics.
- Hono public AP routes for actors, collections, objects, and Create activity dereference.
- WebFinger `self` links and aliases.
- Python persisted actor rows: `bridge_actor_keys`, `users`, and `local_communities` URL/key owner fields.
- Durable local content and activity rows: `message_mappings`, `published_activity_objects`, `user_actor_content_mutations`, `user_actor_outbound_sources`, local-community thread/message/relay tables, and legacy group tables where IDs are locally owned.
- Stale remote relationship state: user followers/follow IDs, remote subscribers, bridge service follow/subscription rows, and outstanding deliveries tied to old local actors.
- Deployment route classification for `/actors/`, `/users/`, `/communities/`, `/c/`, `/inbox`, and `/activitypub/`.
- Secret-boundary consistency: key ID, actor URL, `publicKey.owner`, and signing actor must migrate together without expanding private-key consumers.

### Stage 5 intentionally owns later User Actor behavior

- End-to-end remote Follow to `/actors/users/{username}/inbox` through public gateway HTTP.
- User follower persistence, Accept(Follow) delivery, followers collection, Undo(Follow), personal-feed Create fanout, profile Update emission, and future user content ID hardening.
- Duplicate Follow idempotency and failed Accept recovery under typed actor URL.

### Stage 6 intentionally owns later community/service behavior

- Remote Follow/Undo to `/actors/communities/{slug}/inbox`, accepted `remote_subscribers`, Community Accept(Follow), local-community followers collection, inbound content addressing, relay/fanout, community content mutation continuity, bridge service Follow/Undo future grammar.
- Lemmy-style remote URLs remain external identities and are not rewritten.

### Stage 7 intentionally owns cleanup

- Delete obsolete route code and tests whose only purpose is old public AP compatibility.
- Update `docs/FEDERATION.md`, route maps, compatibility catalog, navigation, and future task/docs surfaces that still describe old actor URL families.
- Add guards/static checks against new local old-URL construction after typed cutover.

## Regression and blind-spot analysis

- The inventory contains both Hono and Fedify route systems because a route may be answered before Fedify middleware. Later route tests must prove the public HTTP path, not just helper output.
- Local and remote URL strings have overlapping shapes. A remote actor like `https://mastodon.example/users/alice` or `https://lemmy.example/c/python` must remain unchanged even though local `/users` and `/c` paths are retired.
- JSON columns can contain local IDs that scalar migration misses. Stage 4 must inspect JSON semantically, not perform global substring replacement.
- Public-key identity is multi-field: actor row URL, key ID, actor document `publicKey.owner`, and signing `keyId` must move together.
- User/community identifier collisions are currently hidden by untyped lookup order. Typed parsing must happen before lookup once Stage 4 changes public routes.
- Raw payload capture is currently shared-inbox-only. Follow may appear to work while Undo/Announce processing fails on actor-specific inboxes if Stage 3/4 do not include raw capture.
- Tests containing old URLs may be either required legacy migration fixtures or stale compatibility assertions. Later stages must classify before editing.
- Dashboard/discovery `/c/` usage is partly product-facing, not necessarily ActivityPub actor compatibility. Stage 4/7 must classify before removing public product surfaces.
- The compatibility catalog currently conflicts with the umbrella's no-compatibility decision. The umbrella is authoritative for this migration; the catalog is documentation debt until Stage 7.

## Validation performed for Stage 1 implementation

The following commands are the required validation set for this documentation-only stage:

```text
git status --short
git diff --check
# Required secret scan from the approved Stage 1 plan against this inventory file.
```

The secret scan was inspected manually as well as by command. This inventory uses table/column names only and contains no dumped secret values, bearer headers, OAuth secrets, Discord tokens, or private key blocks.

Optional runtime checks were not required for Stage 1 and were not used as proof of behavior because this stage changes no runtime code. `fedify-gateway/node_modules` is not present in this checkout, so gateway TypeScript checks would require dependency installation unrelated to this inventory-only stage.
