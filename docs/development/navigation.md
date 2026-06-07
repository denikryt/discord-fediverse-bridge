# Development navigation

This document is a task-oriented reading guide for maintainers changing or debugging one feature area. It owns practical file-reading order and short notes, not full architecture explanations.

## Startup wiring
1. `src/app.py` — process composition.
2. `src/runtime.py` — shared services.
3. `src/config.py` — environment contract.

## Registration
1. `src/commands/register.py` — guild-only command entry after the guild allowlist guard.
2. `src/http_api.py` — browser/OAuth routes.
3. `src/registration_service.py` — session and user creation.
4. `src/db/database.py` — user/session persistence.

## Discord slash commands
1. `src/discord_bot.py` — Discord adapter and command registration.
2. `src/operations/common_preconditions.py` — shared command-access input, atomic DiscordOps preconditions, and named policy compositions.
   `Precondition.name` is the emitted failure reason, so project-owned names must describe the rejection state directly.
3. `src/commands/guild_guard.py` — Discord evaluation and rejection-presentation adapter for those policies.
4. `src/commands/` — command adapters.
5. `src/operations/` — business logic.

## Remote community subscription
1. `src/commands/subscribe.py` for `/subscribe-community` command metadata, option descriptions, autocomplete source selection, Lemmyverse cache wiring, and defensive raw interaction reads for `instance_domain`; 2. `src/commands/subscribe_community_handler.py` for submit-flow orchestration, allowlist checks, community resolution, numeric-id backfill, optional forum-channel placement delegation, operation dispatch, cleanup, directory snapshots, no-ping initiator mentions, and success-message formatting; 3. `src/community_labels.py` for compact `slug@instance` labels used by subscription command output; 4. `src/discord_forum_placement.py` for selected-channel availability, auto-created forum channels, Manage Channels error mapping, and best-effort cleanup; 5. `src/lemmyverse_communities.py` for disk-backed global Lemmyverse autocomplete cache, lazy background refresh, retry handling, monthly-active-user ranking, and Fediverse choice labels; 6. `src/community_discovery.py` for direct-instance autocomplete labels and selected community resolution; 7. `src/operations/subscribe.py`; 8. `src/db/repositories/remote_subscriptions.py`; 9. `src/db/repositories/bridge_actor_follows.py`; 10. `src/lemmy_client.py`; 11. `src/fedify_gateway_client.py`; 12. `fedify-gateway/src/federation-outbound.ts`.

## Remote community unsubscribe
1. `src/commands/unsubscribe.py` for `/unsubscribe-channel` response formatting, no-ping initiator mentions, and remote/local subscriber dispatch; 2. `src/community_labels.py` for compact `slug@instance` labels; 3. `src/operations/unsubscribe.py`; 4. `src/db/repositories/remote_subscriptions.py`; 5. `src/db/repositories/bridge_actor_follows.py`; 6. `src/fedify_gateway_client.py`; 7. `fedify-gateway/src/server.ts`.

## Discord -> remote ActivityPub publish
1. `src/discord_bot.py`; 2. `src/discord_event_router.py`; 3. `src/community_sync/runtime.py`; 4. `src/db/repositories/legacy_lemmy_mappings.py`; 5. `src/db/repositories/discord_fanout_groups.py`; 6. `src/content_sync/outbound_publish.py`; 7. `src/fedify_gateway_client.py`.

## Remote ActivityPub -> Discord fanout

For inbound observability, start with `src/inbound_activity_outcomes.py` for the stable semantic vocabulary, `src/activitypub_handlers.py` and the two runtime modules for classification decisions, `src/http_api.py` for receipt lifecycle orchestration, and `src/db/repositories/event_receipts.py` for atomic persistence.
1. `fedify-gateway/src/federation.ts`; 2. `fedify-gateway/src/normalize.ts`; 3. `fedify-gateway/src/python-bridge.ts`; 4. `src/activitypub_handlers.py`; 5. `src/community_sync/discord_fanout.py`; 6. `src/db/repositories/discord_fanout_groups.py`; 7. `src/db/repositories/legacy_lemmy_mappings.py`.

## Local community creation
1. `src/operations/common_preconditions.py` for shared access inputs, atomic preconditions, and named policy compositions; 2. `src/commands/guild_guard.py` for Discord evaluation and rejection presentation; 3. `src/commands/create_community.py` for the `/create_community` modal launcher, modal submit validation, optional forum-channel selection, and snapshot timing; 4. `src/discord_forum_placement.py` for selected-channel availability, auto-created forum channels, Manage Channels error mapping, and cleanup after later failures; 5. `src/operations/create_community.py` for domain creation by registered users; 6. `src/local_communities/service.py`; 7. `src/management_actions.py` for transactional creation plus audit; 8. `src/db/repositories/local_communities.py`; 9. `src/management_audit_recorder.py`; 10. `src/management_audit.py`; 11. `src/db/repositories/management_audit_events.py`; 12. `src/db/database.py`; 13. `src/local_community_permissions.py` for owner/super-admin management checks after creation.

## Local community actor rendering
1. `fedify-gateway/src/server.ts`; 2. `fedify-gateway/src/actor-store.ts`; 3. `fedify-gateway/src/actors.ts`; 4. `fedify-gateway/src/webfinger.ts`.



## Local community metadata and lifecycle editing
1. `src/commands/edit_community.py` — slash command, community autocomplete, Discord modal adapter, and status select UI.
2. `src/operations/edit_community.py` — owner/super-admin authorization, validation, lifecycle status validation, and persistence.
3. `src/local_community_lifecycle.py` — active/disabled lifecycle decisions shared by command and runtime gates.
4. `src/management_actions.py` — transaction boundary for settings mutation plus success audit.
5. `src/db/repositories/local_communities.py` — settings persistence, changed-field deltas, and active/manageable autocomplete repository methods.
6. `src/management_audit_recorder.py` — semantic audit-row construction for success and forbidden management outcomes.
7. `src/management_audit.py`; `src/db/repositories/management_audit_events.py` — v1 audit vocabulary, canonical JSON payloads, and low-level audit-row insertion.
8. `src/local_communities/service.py` — shared display-name and summary normalization rules.


## Local community user bans
1. `src/commands/ban_user.py`; `src/commands/unban_user.py`; `src/commands/list_banned_users.py` — Discord slash command adapters and autocomplete.
2. `src/operations/ban_user.py`; `src/operations/unban_user.py`; `src/operations/list_banned_users.py` — `discordops` preconditions, guild scoping, owner/super-admin authorization, handle validation, list formatting, and unban behavior.
3. `src/fediverse_identity.py` — command handle normalization and hot-path actor URL extraction.
4. `src/local_community_permissions.py` — command-side owner/super-admin and guild-access policy.
5. `src/management_actions.py` — transaction boundary for ban/unban mutation plus success audit.
6. `src/db/repositories/community_actor_bans.py` — scoped active-ban persistence, inactive-row reactivation deltas, list/count/deactivate helpers.
7. `src/management_audit_recorder.py` — semantic audit-row construction for ban/unban success and forbidden outcomes.
8. `src/management_audit.py`; `src/db/repositories/management_audit_events.py` — v1 audit vocabulary, canonical JSON payloads, and low-level audit-row insertion helpers.
9. `src/community_moderation.py` — inbound ban resolution before local-community side effects.
10. `src/activitypub_handlers.py` — dispatch integration after receipt/idempotency begins.
11. `tests/behavior/test_local_community_user_ban_scenarios.py`; 12. `tests/operations/test_ban_user_operation.py`; 13. `tests/operations/test_unban_user_operation.py`; 14. `tests/operations/test_list_banned_users_operation.py`; 15. `tests/operations/test_management_audit_events.py`; 16. `tests/commands/test_ban_user_command.py`; 17. `tests/commands/test_unban_user_command.py`; 18. `tests/commands/test_list_banned_users_command.py`; 19. `tests/test_local_community_permissions.py`; 20. `tests/test_fediverse_identity.py`.

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

### Guild invite publication

`src/commands/publish_guild_invite.py` and `src/commands/remove_guild_invite.py` are thin Discord adapters. `src/operations/publish_guild_invite.py` and `src/operations/remove_guild_invite.py` declare eligibility through DiscordOps and own Discord invite side effects plus transactional publication state. `src/operations/guild_invite_lock.py` serializes publish/remove mutations per guild. `src/db/repositories/guild_invite_publications.py` owns the single current invite row per guild, and `src/dashboard.py` exposes only the public invite URL on existing guild cards.

## Actor keys and backups

- `src/actor_key_service.py` bootstraps and resolves local actor signing keys.
- `src/db/repositories/bridge_actor_keys.py` persists the bridge service actor keypair.
- `src/db/backup.py` creates, retains, and restores validated SQLite snapshots.
