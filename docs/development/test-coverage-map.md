# Test coverage map

This document is a short, feature-oriented index of the test suite. It helps maintainers find existing coverage before adding a new test. It intentionally links to files rather than line numbers or individual test functions, because those change more often.

## Test layers

- **Behavior** — user or platform action in a defined system state, verified through observable runtime effects.
- **Operation** — command business rules, precondition ordering, state changes, and audit boundaries.
- **Command** — Discord adapter behavior such as inputs, ephemeral responses, and autocomplete.
- **Contract / infrastructure** — persistence, protocol, configuration, migration, and integration contracts.

## Registration and user identity

- Registration scenarios, duplicate handling, and user-facing registration outcomes — [`tests/behavior/test_registration_scenarios.py`](../../tests/behavior/test_registration_scenarios.py)
- Browser/OAuth registration flow and actor identity creation — [`tests/test_registration_flow.py`](../../tests/test_registration_flow.py)
- Registration command adapter — [`tests/commands/test_register_command.py`](../../tests/commands/test_register_command.py)
- Fediverse handle and actor URL rules — [`tests/test_fediverse_identity.py`](../../tests/test_fediverse_identity.py)
- User identity backup and restore tooling — [`tests/test_user_identity_dump.py`](../../tests/test_user_identity_dump.py)

## Community discovery and subscriptions

- Remote subscription lifecycle and accepted/pending behavior — [`tests/behavior/test_subscription_scenarios.py`](../../tests/behavior/test_subscription_scenarios.py)
- Follow/Accept subscription protocol flow — [`tests/test_follow_subscription_flow.py`](../../tests/test_follow_subscription_flow.py)
- Cross-stage register, subscribe, accept, publish, retry, and echo suppression — [`tests/behavior/test_cross_stage_scenarios.py`](../../tests/behavior/test_cross_stage_scenarios.py)
- Unified local/remote community discovery — [`tests/behavior/test_unified_community_discovery_scenarios.py`](../../tests/behavior/test_unified_community_discovery_scenarios.py)
- Community resolution rules and fallback behavior — [`tests/test_community_discovery_resolution.py`](../../tests/test_community_discovery_resolution.py)
- Subscribe business rules — [`tests/operations/test_subscribe_operation.py`](../../tests/operations/test_subscribe_operation.py)
- Subscribe Discord adapter and autocomplete — [`tests/commands/test_subscribe_command.py`](../../tests/commands/test_subscribe_command.py)
- Unsubscribe behavior, retry, and cleanup — [`tests/behavior/test_unsubscribe_retry_scenarios.py`](../../tests/behavior/test_unsubscribe_retry_scenarios.py), [`tests/operations/test_unsubscribe_operation.py`](../../tests/operations/test_unsubscribe_operation.py), [`tests/commands/test_unsubscribe_command.py`](../../tests/commands/test_unsubscribe_command.py)
- Subscription listing operation and command output — [`tests/operations/test_list_subscriptions_operation.py`](../../tests/operations/test_list_subscriptions_operation.py), [`tests/commands/test_list_subscriptions_command.py`](../../tests/commands/test_list_subscriptions_command.py)
- Lemmyverse autocomplete cache, parsing, ranking, and retries — [`tests/test_lemmyverse_communities.py`](../../tests/test_lemmyverse_communities.py)

## Discord to Fediverse publishing

- Basic outbound post/comment publish behavior — [`tests/behavior/test_publish_scenarios.py`](../../tests/behavior/test_publish_scenarios.py)
- Discord publish routing and side effects — [`tests/test_discord_publish_flow.py`](../../tests/test_discord_publish_flow.py)
- Thread fanout to sibling subscriptions — [`tests/test_phase2_fanout_scenarios.py`](../../tests/test_phase2_fanout_scenarios.py)
- Fail-closed routing metadata and per-target fanout isolation — [`tests/test_policy_routing_metadata.py`](../../tests/test_policy_routing_metadata.py)
- Message fanout to sibling threads — [`tests/test_phase3_message_fanout_scenarios.py`](../../tests/test_phase3_message_fanout_scenarios.py)
- Reply and parent preservation — [`tests/test_phase4_reply_preservation.py`](../../tests/test_phase4_reply_preservation.py)
- Bidirectional mirror-message publishing and loop prevention — [`tests/test_phase9_bidirectional_mirror_messages.py`](../../tests/test_phase9_bidirectional_mirror_messages.py)

## Fediverse to Discord inbound delivery

- Inbound posts/comments, fanout, dedup, deferred delivery, and partial failure — [`tests/behavior/test_inbound_scenarios.py`](../../tests/behavior/test_inbound_scenarios.py)
- Comment-before-post backfill and retry — [`tests/behavior/test_inbound_comment_backfill.py`](../../tests/behavior/test_inbound_comment_backfill.py)
- Shared Discord thread/message groups for inbound ActivityPub — [`tests/test_phase5_inbound_ap_shared_groups.py`](../../tests/test_phase5_inbound_ap_shared_groups.py)
- Inbound outcome vocabulary and receipt state — [`tests/test_inbound_activity_outcomes.py`](../../tests/test_inbound_activity_outcomes.py)
- Skipping activity for unsubscribed communities — [`tests/behavior/test_unsubscribed_inbound_activity_skip.py`](../../tests/behavior/test_unsubscribed_inbound_activity_skip.py)

## Local communities

- Community creation, validation, ownership, and schema migration — [`tests/behavior/test_local_community_registration_scenarios.py`](../../tests/behavior/test_local_community_registration_scenarios.py)
- Create-community operation and Discord adapter — [`tests/commands/test_create_community_command.py`](../../tests/commands/test_create_community_command.py)
- Local-community outbound post/comment publishing — [`tests/behavior/test_local_community_publish_scenarios.py`](../../tests/behavior/test_local_community_publish_scenarios.py)
- Local-community runtime routing and handler integration — [`tests/test_community_runtime_scenarios.py`](../../tests/test_community_runtime_scenarios.py)
- Remote followers, inbound posts/replies, follow/unfollow, and idempotency — [`tests/behavior/test_local_community_inbound_scenarios.py`](../../tests/behavior/test_local_community_inbound_scenarios.py)
- Local-community relay to remote followers — [`tests/behavior/test_local_community_remote_fanout_scenarios.py`](../../tests/behavior/test_local_community_remote_fanout_scenarios.py)
- Disabled-community runtime restrictions — [`tests/behavior/test_local_community_disabled_scenarios.py`](../../tests/behavior/test_local_community_disabled_scenarios.py)
- Metadata/status editing behavior — [`tests/behavior/test_local_community_edit_metadata_scenarios.py`](../../tests/behavior/test_local_community_edit_metadata_scenarios.py), [`tests/operations/test_edit_community_operation.py`](../../tests/operations/test_edit_community_operation.py), [`tests/commands/test_edit_community_command.py`](../../tests/commands/test_edit_community_command.py)
- Community owner and super-admin permission rules — [`tests/test_local_community_permissions.py`](../../tests/test_local_community_permissions.py)

## Local subscriber Discord surfaces

- Local subscription control-plane behavior — [`tests/behavior/test_local_subscriber_stage1_scenarios.py`](../../tests/behavior/test_local_subscriber_stage1_scenarios.py)
- Host-originated fanout to local subscriber surfaces — [`tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`](../../tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py)
- Subscriber-originated publishing, headers, reply mapping, and retries — [`tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py`](../../tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py)
- Participant edit/delete propagation — [`tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py`](../../tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py)
- Discord surface creation and placement — [`tests/behavior/test_local_community_surface_stage2_scenarios.py`](../../tests/behavior/test_local_community_surface_stage2_scenarios.py), [`tests/test_discord_forum_placement.py`](../../tests/test_discord_forum_placement.py)

## User bans and moderation

- Community-scoped inbound moderation behavior — [`tests/behavior/test_local_community_user_ban_scenarios.py`](../../tests/behavior/test_local_community_user_ban_scenarios.py)
- Local/global ban creation, DM notification, enforcement, fail-closed behavior, canonical headers, and migration — [`tests/test_user_bans_plan93.py`](../../tests/test_user_bans_plan93.py)
- Ban, unban, and list business rules — [`tests/operations/test_ban_user_operation.py`](../../tests/operations/test_ban_user_operation.py), [`tests/operations/test_unban_user_operation.py`](../../tests/operations/test_unban_user_operation.py), [`tests/operations/test_list_banned_users_operation.py`](../../tests/operations/test_list_banned_users_operation.py)
- Ban command adapters and ephemeral output — [`tests/commands/test_ban_user_command.py`](../../tests/commands/test_ban_user_command.py), [`tests/commands/test_unban_user_command.py`](../../tests/commands/test_unban_user_command.py), [`tests/commands/test_list_banned_users_command.py`](../../tests/commands/test_list_banned_users_command.py)
- Management audit events and transaction outcomes — [`tests/operations/test_management_audit_events.py`](../../tests/operations/test_management_audit_events.py)

## Edit, delete, dedup, and retry

- Outbound and inbound edit/delete synchronization — [`tests/test_phase8_edit_delete_sync.py`](../../tests/test_phase8_edit_delete_sync.py)
- Local-community edit/delete synchronization — [`tests/behavior/test_local_community_edit_delete_scenarios.py`](../../tests/behavior/test_local_community_edit_delete_scenarios.py)
- Deduplication, replay suppression, uniqueness, and mirror guards — [`tests/test_phase6_dedup_hardening.py`](../../tests/test_phase6_dedup_hardening.py), [`tests/test_end_to_end_dedup_flow.py`](../../tests/test_end_to_end_dedup_flow.py)

## Dashboard and Discord directory data

- Dashboard payloads, visibility, health, and static routes — [`tests/behavior/test_dashboard_scenarios.py`](../../tests/behavior/test_dashboard_scenarios.py)
- Discord guild/channel snapshot creation and refresh — [`tests/behavior/test_discord_directory_snapshot_scenarios.py`](../../tests/behavior/test_discord_directory_snapshot_scenarios.py)
- Community labels used in user-facing output — [`tests/test_community_labels.py`](../../tests/test_community_labels.py)

## Guild invite publication

- End-to-end publish/remove behavior and failure handling — [`tests/behavior/test_guild_invite_publication_scenarios.py`](../../tests/behavior/test_guild_invite_publication_scenarios.py)
- Publish/remove operation rules — [`tests/operations/test_guild_invite_operations.py`](../../tests/operations/test_guild_invite_operations.py)
- Discord command adapters — [`tests/commands/test_publish_guild_invite_command.py`](../../tests/commands/test_publish_guild_invite_command.py), [`tests/commands/test_remove_guild_invite_command.py`](../../tests/commands/test_remove_guild_invite_command.py)

## Access policy and command guards

- Shared command-access policy behavior — [`tests/test_command_access_policy.py`](../../tests/test_command_access_policy.py)
- Guild guard and Discord rejection presentation — [`tests/commands/test_guild_guard.py`](../../tests/commands/test_guild_guard.py)
- Guild allowlist configuration — [`tests/test_guild_allowlist_settings.py`](../../tests/test_guild_allowlist_settings.py)

## Persistence, configuration, gateway, and deployment

- Database federation identity and actor persistence — [`tests/test_db_federation_identity.py`](../../tests/test_db_federation_identity.py)
- SQLite versions, startup preservation, DDL ordering, rollback and concurrent upgrades — [`tests/test_migration_safety.py`](../../tests/test_migration_safety.py)
- Schema cleanup and migration idempotency — [`tests/test_stage5_schema_cleanup.py`](../../tests/test_stage5_schema_cleanup.py)
- Removal of legacy remote-subscriber naming compatibility — [`tests/test_stage1_remote_subscriber_naming.py`](../../tests/test_stage1_remote_subscriber_naming.py)
- SQLite backups — [`tests/behavior/test_sqlite_backup_scenarios.py`](../../tests/behavior/test_sqlite_backup_scenarios.py)
- Internal Python/Fedify API contract — [`tests/test_internal_fedify_api.py`](../../tests/test_internal_fedify_api.py)
- Federation policy and allowlist handlers — [`tests/test_federation_policy.py`](../../tests/test_federation_policy.py), [`tests/test_federation_allowlist_handlers.py`](../../tests/test_federation_allowlist_handlers.py)
- Actor key bootstrap — [`tests/test_bridge_actor_key_bootstrap.py`](../../tests/test_bridge_actor_key_bootstrap.py)
- Public URL and OAuth redirect configuration — [`tests/test_public_base_url_config.py`](../../tests/test_public_base_url_config.py)
- Discord OAuth client request/response contract — [`tests/test_discord_oauth_client.py`](../../tests/test_discord_oauth_client.py)
- Docker deployment contract — [`tests/test_docker_deployment.py`](../../tests/test_docker_deployment.py)
- Project version contract — [`tests/test_project_version.py`](../../tests/test_project_version.py)

## Finding existing coverage

Search by feature term, observable message, reason code, or persisted effect before adding a test:

```bash
pytest --collect-only -q | rg '<feature|action|result>'
rg -n '<message|reason_code|domain term>' tests
```

Start with the **Behavior** files for user-visible bridge behavior, then check **Operation** and **Command** files for policy and adapter-specific coverage.

## User profile/media Stage 1

Behavior coverage lives in `test_user_profile_persistence_scenarios.py`, `test_profile_media_scenarios.py`, and `test_profile_media_backup_scenarios.py`. It covers fallback defaults, atomic metadata validation, SQLite invariants, canonical image publication, DB-backed HTTP delivery, and complete DB-plus-media backup/restore.

## Platform model Stage 01

- Personal Feed service/repository agreement on physical claims and target-local parent resolution during upgrade — [`tests/test_stage01_simplicity_regressions.py`](../../tests/test_stage01_simplicity_regressions.py).

- Generated roles, owner exclusivity, scoped identity, parent-channel FKs, recipient selection, and retry CAS — [`tests/test_platform_data_model.py`](../../tests/test_platform_data_model.py).
- Claim kind admission for all owner types on both connectors, schema-only `create_all()` and explicit application bootstrap — [`tests/test_stage01_review_regressions.py`](../../tests/test_stage01_review_regressions.py).
- Isolated historical platform-input transformation, fresh/current schema parity, populated cascade preservation, failures during rebuild, concurrent migration runners, and FK enforcement after commit — [`tests/test_platform_data_model_migration_stage01.py`](../../tests/test_platform_data_model_migration_stage01.py).
- Mixed Discord/Fluxer post/comment routing, explicit/implicit Accept with global status promotion and Discord-only DMs, local publish fanout, HTTP dashboard, guild/guildless subscription lists, and Personal Feed view/mutation isolation — [`tests/behavior/test_platform_data_model_stage01.py`](../../tests/behavior/test_platform_data_model_stage01.py).
- `tests/fixtures/platform_v1_schema.sql` and `tests/fixtures/platform_v1_models.py` preserve the independent v1 baseline; current ORM metadata must not generate the upgrade fixture.

- Released v0→current v1, source recognition, exact rollback, restored FK enforcement, semantic DDL parity, concurrent startup and migrated runtime actions — [`tests/test_legacy_v0_to_v1_migration.py`](../../tests/test_legacy_v0_to_v1_migration.py).

- Complete generated-role column admission: appended expression, wrapper, VIRTUAL mode rejection, formatting tolerance and unchanged database on rejection — [`tests/test_current_v1_generated_role_validation.py`](../../tests/test_current_v1_generated_role_validation.py).

- Real released AP namespaces, selective relationship reset, linked routing/JSON conversion, signed-payload/collision rejection, actor API, concurrent cutover and retained-claim retry — [`tests/test_legacy_identity_cutover.py`](../../tests/test_legacy_identity_cutover.py).
