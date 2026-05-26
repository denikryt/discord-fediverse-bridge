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
4. `src/db.py` — user/session persistence.

## Discord slash commands
1. `src/discord_bot.py` — Discord adapter.
2. `src/commands/` — command adapters.
3. `src/operations/` — business logic.

## Remote community subscription
1. `src/commands/subscribe.py`; 2. `src/operations/subscribe.py`; 3. `src/lemmy_client.py`; 4. `src/fedify_gateway_client.py`; 5. `fedify-gateway/src/federation-outbound.ts`.

## Remote community unsubscribe
1. `src/commands/unsubscribe.py`; 2. `src/operations/unsubscribe.py`; 3. `src/fedify_gateway_client.py`; 4. `fedify-gateway/src/server.ts`.

## Discord -> remote ActivityPub publish
1. `src/discord_bot.py`; 2. `src/discord_event_router.py`; 3. `src/community_sync/runtime.py`; 4. `src/content_sync/outbound_publish.py`; 5. `src/fedify_gateway_client.py`.

## Remote ActivityPub -> Discord fanout
1. `fedify-gateway/src/federation.ts`; 2. `fedify-gateway/src/normalize.ts`; 3. `fedify-gateway/src/python-bridge.ts`; 4. `src/activitypub_handlers.py`; 5. `src/community_sync/discord_fanout.py`.

## Local community creation
1. `src/commands/create_community.py`; 2. `src/operations/create_community.py`; 3. `src/local_communities/service.py`; 4. `src/db.py`.

## Local community actor rendering
1. `fedify-gateway/src/server.ts`; 2. `fedify-gateway/src/actor-store.ts`; 3. `fedify-gateway/src/actors.ts`; 4. `fedify-gateway/src/webfinger.ts`.

## Remote Follow handling for local communities
1. `fedify-gateway/src/federation.ts`; 2. `fedify-gateway/src/normalize.ts`; 3. `src/activitypub_handlers.py`; 4. `src/local_communities/runtime.py`; 5. `src/fedify_gateway_client.py`.

## Local subscriber control-plane
1. `src/commands/subscribe.py`; 2. `src/operations/subscribe_local_community.py`; 3. `src/commands/unsubscribe.py`; 4. `src/operations/unsubscribe_local_community.py`; 5. `src/db.py`.

## Local community relay fanout
1. `src/local_communities/federation_fanout.py`; 2. `src/local_communities/runtime.py`; 3. `src/local_communities/delivery_mapping.py`; 4. `src/local_communities/reply_mapping.py`; 5. `src/db.py`; 6. `fedify-gateway/src/federation-outbound.ts`.

## Edit/delete propagation
1. `src/discord_event_router.py`; 2. `src/content_sync/edit_delete.py`; 3. `src/community_sync/edit_delete.py`; 4. `src/local_communities/runtime.py`; 5. `src/local_communities/delivery_mapping.py`; 6. `src/db.py`; 7. `fedify-gateway/src/server.ts`.

## Deduplication and idempotency
1. `src/activitypub_handlers.py`; 2. `src/content_sync/persistence.py`; 3. `src/db.py`; 4. `fedify-gateway/src/normalize.ts`.

## Database schema
1. `src/models.py`; 2. `src/db.py`; 3. `docs/architecture/database-map.md`.
Stage 2 local-community work also requires reading the canonical-vs-surface split in `LocalCommunityThread`, `LocalCommunityMessage`, `LocalCommunityThreadSurface`, and `LocalCommunityMessageSurface`.

## Gateway route changes
1. `fedify-gateway/src/server.ts`; 2. `fedify-gateway/src/types.ts`; 3. `src/fedify_gateway_client.py`; 4. `docs/architecture/http-routes.md`.

## Public Python route changes
1. `src/http_api.py`; 2. `src/app.py`; 3. `docs/architecture/http-routes.md`.
