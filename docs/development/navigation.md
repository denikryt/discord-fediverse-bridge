# Development navigation

This document is a task-oriented reading guide for maintainers changing or debugging one feature area. It owns practical file-reading order and short notes, not full architecture explanations.

## Startup wiring
1. `src/app.py` — process composition.
2. `src/runtime.py` — shared services.
3. `src/config.py` — environment contract.

## Registration
1. `src/commands/register.py` — command entry.
2. `src/http_api.py` — browser/OAuth routes.
3. `src/registration_service.py` — session and user creation.
4. `src/db/database.py` — user/session persistence.

## Discord slash commands
1. `src/discord_bot.py` — Discord adapter.
2. `src/commands/` — command adapters.
3. `src/operations/` — business logic.

## Remote community subscription
1. `src/commands/subscribe.py`; 2. `src/operations/subscribe.py`; 3. `src/db/repositories/remote_subscriptions.py`; 4. `src/db/repositories/bridge_actor_follows.py`; 5. `src/lemmy_client.py`; 6. `src/fedify_gateway_client.py`; 7. `fedify-gateway/src/federation-outbound.ts`.

## Remote community unsubscribe
1. `src/commands/unsubscribe.py`; 2. `src/operations/unsubscribe.py`; 3. `src/db/repositories/remote_subscriptions.py`; 4. `src/db/repositories/bridge_actor_follows.py`; 5. `src/fedify_gateway_client.py`; 6. `fedify-gateway/src/server.ts`.

## Discord -> remote ActivityPub publish
1. `src/discord_bot.py`; 2. `src/discord_event_router.py`; 3. `src/community_sync/runtime.py`; 4. `src/content_sync/outbound_publish.py`; 5. `src/fedify_gateway_client.py`.

## Remote ActivityPub -> Discord fanout
1. `fedify-gateway/src/federation.ts`; 2. `fedify-gateway/src/normalize.ts`; 3. `fedify-gateway/src/python-bridge.ts`; 4. `src/activitypub_handlers.py`; 5. `src/community_sync/discord_fanout.py`.

## Local community creation
1. `src/commands/create_community.py`; 2. `src/operations/create_community.py`; 3. `src/local_communities/service.py`; 4. `src/db/database.py`; 5. `src/db/repositories/local_communities.py`.

## Local community actor rendering
1. `fedify-gateway/src/server.ts`; 2. `fedify-gateway/src/actor-store.ts`; 3. `fedify-gateway/src/actors.ts`; 4. `fedify-gateway/src/webfinger.ts`.

## Remote Follow handling for local communities
1. `fedify-gateway/src/federation.ts`; 2. `fedify-gateway/src/normalize.ts`; 3. `src/activitypub_handlers.py`; 4. `src/local_communities/runtime.py`; 5. `src/fedify_gateway_client.py`.

## Local subscriber control-plane
1. `src/commands/subscribe.py`; 2. `src/operations/subscribe_local_community.py`; 3. `src/commands/unsubscribe.py`; 4. `src/operations/unsubscribe_local_community.py`; 5. `src/db/database.py`; 6. `src/db/repositories/local_subscribers.py`.

## Local community relay fanout
1. `src/local_communities/federation_fanout.py`; 2. `src/local_communities/runtime.py`; 3. `src/local_communities/delivery_mapping.py`; 4. `src/local_communities/reply_mapping.py`; 5. `src/db/repositories/local_community_relay.py`; 6. `src/db/database.py` temporary wrappers; 7. `fedify-gateway/src/federation-outbound.ts`.

## Local subscriber Discord fanout
1. `src/local_communities/participant_routing.py`; 2. `src/local_communities/runtime.py`; 3. `src/local_communities/discord_fanout.py`; 4. `src/local_communities/reply_mapping.py`; 5. `src/db/repositories/local_community_content.py`; 6. `src/db/repositories/local_community_surfaces.py`; 7. `src/db/repositories/local_subscribers.py`; 8. `src/db/database.py` temporary wrappers; 9. `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`; 10. `tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py`.

## Edit/delete propagation
1. `src/discord_event_router.py`; 2. `src/content_sync/edit_delete.py`; 3. `src/community_sync/edit_delete.py`; 4. `src/local_communities/runtime.py`; 5. `src/local_communities/delivery_mapping.py`; 6. `src/db/database.py`; 7. `fedify-gateway/src/server.ts`.

## Deduplication and idempotency
1. `src/activitypub_handlers.py`; 2. `src/content_sync/persistence.py`; 3. `src/db/repositories/event_receipts.py`; 4. `src/db/database.py` temporary wrappers; 5. `fedify-gateway/src/normalize.ts`.

## Database schema and persistence navigation
1. `src/models.py`; 2. `src/db/database.py`; 3. `src/db/repositories/`; 4. `src/db/schema.py`; 5. `src/db/migrations.py`; 6. `docs/architecture/database-map.md`; 7. `docs/architecture/database-method-inventory.md`.

Use `docs/architecture/database-map.md` for table ownership and invariants. Use `docs/architecture/database-method-inventory.md` before changing `src/db/database.py`; it maps current `Database` methods to target repository groups, primary call sites, relevant tests, and extraction risks. Schema bootstrap lives in `src/db/schema.py`; additive migration checks live in `src/db/migrations.py`. Stage 3 local-community persistence lives in `src/db/repositories/local_communities.py`, `remote_subscribers.py`, `local_subscribers.py`, `local_community_content.py`, `local_community_surfaces.py`, and `local_community_relay.py`; Stage 4 remote subscription persistence lives in `remote_subscriptions.py` and `bridge_actor_follows.py`; Stage 5 users, registration sessions, and event receipts live in `users.py`, `registration_sessions.py`, and `event_receipts.py`; matching `Database.*` methods are temporary forwarding wrappers until Stage 8. Stage 2 local-community work also requires reading the canonical-vs-surface split in `LocalCommunityThread`, `LocalCommunityMessage`, `LocalCommunityThreadSurface`, and `LocalCommunityMessageSurface`.

## Gateway route changes
1. `fedify-gateway/src/server.ts`; 2. `fedify-gateway/src/types.ts`; 3. `src/fedify_gateway_client.py`; 4. `docs/architecture/http-routes.md`.

## Public Python route changes
1. `src/http_api.py`; 2. `src/app.py`; 3. `docs/architecture/http-routes.md`.

## Local community participant edit/delete
1. `src/discord_event_router.py` — raw Discord edit/delete ownership check.
2. `src/local_communities/runtime.py` — canonical AP object resolution and gateway Update/Delete calls.
3. `src/local_communities/discord_fanout.py` — per-surface local Discord edit/delete fanout.
4. `src/local_communities/delivery_mapping.py` — surface/canonical lookup helpers.
5. `src/content_sync/edit_delete.py` — shared Discord message edit/delete edge helpers.
