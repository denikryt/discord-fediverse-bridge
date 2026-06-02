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
1. `src/discord_bot.py`; 2. `src/discord_event_router.py`; 3. `src/community_sync/runtime.py`; 4. `src/db/repositories/legacy_lemmy_mappings.py`; 5. `src/db/repositories/discord_fanout_groups.py`; 6. `src/content_sync/outbound_publish.py`; 7. `src/fedify_gateway_client.py`.

## Remote ActivityPub -> Discord fanout
1. `fedify-gateway/src/federation.ts`; 2. `fedify-gateway/src/normalize.ts`; 3. `fedify-gateway/src/python-bridge.ts`; 4. `src/activitypub_handlers.py`; 5. `src/community_sync/discord_fanout.py`; 6. `src/db/repositories/discord_fanout_groups.py`; 7. `src/db/repositories/legacy_lemmy_mappings.py`.

## Local community creation
1. `src/commands/create_community.py`; 2. `src/operations/create_community.py`; 3. `src/local_communities/service.py`; 4. `src/db/repositories/local_communities.py`; 5. `src/management_audit.py`; 6. `src/db/repositories/management_audit_events.py`; 7. `src/db/database.py`; 8. `src/local_community_permissions.py` for owner/super-admin management checks.

## Local community actor rendering
1. `fedify-gateway/src/server.ts`; 2. `fedify-gateway/src/actor-store.ts`; 3. `fedify-gateway/src/actors.ts`; 4. `fedify-gateway/src/webfinger.ts`.



## Local community metadata and lifecycle editing
1. `src/commands/edit_community.py` — slash command, community autocomplete, Discord modal adapter, and status select UI.
2. `src/operations/edit_community.py` — owner/super-admin authorization, validation, lifecycle status validation, and persistence.
3. `src/local_community_lifecycle.py` — active/disabled lifecycle decisions shared by command and runtime gates.
4. `src/db/repositories/local_communities.py` — settings update, transactionally coupled audit writes, and active/manageable autocomplete repository methods.
5. `src/management_audit.py`; `src/db/repositories/management_audit_events.py` — changed-field audit payloads and audit-row insertion.
6. `src/local_communities/service.py` — shared display-name and summary normalization rules.


## Local community user bans
1. `src/commands/ban_user.py`; `src/commands/unban_user.py`; `src/commands/list_banned_users.py` — Discord slash command adapters and autocomplete.
2. `src/operations/ban_user.py`; `src/operations/unban_user.py`; `src/operations/list_banned_users.py` — `discordops` preconditions, guild scoping, owner/super-admin authorization, handle validation, list formatting, and unban behavior.
3. `src/fediverse_identity.py` — command handle normalization and hot-path actor URL extraction.
4. `src/local_community_permissions.py` — command-side owner/super-admin and guild-access policy.
5. `src/db/repositories/community_actor_bans.py` — scoped active-ban persistence, inactive-row reactivation, list/count/deactivate helpers, and transactionally coupled ban/unban audit writes.
6. `src/management_audit.py`; `src/db/repositories/management_audit_events.py` — v1 audit vocabulary, canonical JSON payloads, and audit-row insertion helpers.
7. `src/community_moderation.py` — inbound ban resolution before local-community side effects.
8. `src/activitypub_handlers.py` — dispatch integration after receipt/idempotency begins.
9. `tests/behavior/test_local_community_user_ban_scenarios.py`; 10. `tests/operations/test_ban_user_operation.py`; 11. `tests/operations/test_unban_user_operation.py`; 12. `tests/operations/test_list_banned_users_operation.py`; 13. `tests/operations/test_management_audit_events.py`; 14. `tests/commands/test_ban_user_command.py`; 15. `tests/commands/test_unban_user_command.py`; 16. `tests/commands/test_list_banned_users_command.py`; 17. `tests/test_local_community_permissions.py`; 18. `tests/test_fediverse_identity.py`.

## Remote Follow handling for local communities
1. `fedify-gateway/src/federation.ts`; 2. `fedify-gateway/src/normalize.ts`; 3. `src/activitypub_handlers.py`; 4. `src/local_communities/runtime.py`; 5. `src/fedify_gateway_client.py`.

## Local subscriber control-plane
1. `src/commands/subscribe.py`; 2. `src/operations/subscribe_local_community.py`; 3. `src/commands/unsubscribe.py`; 4. `src/operations/unsubscribe_local_community.py`; 5. `src/db/database.py`; 6. `src/db/repositories/local_subscribers.py`.

## Local community relay fanout
1. `src/local_communities/federation_fanout.py`; 2. `src/local_communities/runtime.py`; 3. `src/local_communities/delivery_mapping.py`; 4. `src/local_communities/reply_mapping.py`; 5. `src/db/repositories/local_community_relay.py`; 6. `src/db/database.py` repository container; 7. `fedify-gateway/src/federation-outbound.ts`.

## Local subscriber Discord fanout
1. `src/local_communities/participant_routing.py`; 2. `src/local_communities/runtime.py`; 3. `src/local_communities/discord_fanout.py`; 4. `src/local_communities/reply_mapping.py`; 5. `src/db/repositories/local_community_content.py`; 6. `src/db/repositories/local_community_surfaces.py`; 7. `src/db/repositories/local_subscribers.py`; 8. `src/db/database.py` repository container; 9. `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`; 10. `tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py`.

## Edit/delete propagation
1. `src/discord_event_router.py`; 2. `src/content_sync/edit_delete.py`; 3. `src/community_sync/edit_delete.py`; 4. `src/local_communities/runtime.py`; 5. `src/local_communities/delivery_mapping.py`; 6. `src/db/database.py`; 7. `fedify-gateway/src/server.ts`.

## Deduplication and idempotency
1. `src/activitypub_handlers.py`; 2. `src/content_sync/persistence.py`; 3. `src/db/repositories/event_receipts.py`; 4. `src/db/repositories/message_mappings.py`; 5. `src/db/repositories/activitypub_objects.py`; 6. `src/db/database.py` repository container; 7. `fedify-gateway/src/normalize.ts`.

## Database schema and persistence navigation
1. `src/models.py`; 2. `src/db/database.py`; 3. `src/db/repositories/`; 4. `src/db/schema.py`; 5. `src/db/migrations.py`; 6. `docs/architecture/database-map.md`; 7. `docs/architecture/database-method-inventory.md`.

Use `docs/architecture/database-map.md` for table ownership and invariants. Use `docs/architecture/database-method-inventory.md` before changing `src/db/database.py`; it maps current `Database` methods to target repository groups, primary call sites, relevant tests, and extraction risks. Schema bootstrap lives in `src/db/schema.py`; additive migration checks live in `src/db/migrations.py`. Stage 3 local-community persistence lives in `src/db/repositories/local_communities.py`, `remote_subscribers.py`, `local_subscribers.py`, `local_community_content.py`, `local_community_surfaces.py`, and `local_community_relay.py`; Stage 4 remote subscription persistence lives in `remote_subscriptions.py` and `bridge_actor_follows.py`; Stage 5 users, registration sessions, and event receipts live in `users.py`, `registration_sessions.py`, and `event_receipts.py`; Stage 6 generic ActivityPub mappings, objects, and actors live in `message_mappings.py`, `activitypub_objects.py`, and `remote_actors.py`; Stage 7 legacy Lemmy mappings and Discord fanout groups live in `legacy_lemmy_mappings.py` and `discord_fanout_groups.py`; domain persistence calls should use these repository properties directly; `Database` keeps only infrastructure methods and repository construction. Local-community work also requires reading the canonical-vs-surface split in `LocalCommunityThread`, `LocalCommunityMessage`, `LocalCommunityThreadSurface`, and `LocalCommunityMessageSurface`.

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
