# Database method inventory

This document is the Stage 0 method-level inventory for the database repository split. It complements `docs/architecture/database-map.md`, which remains the table-level schema map. This file maps each public `Database` method to its intended repository owner, primary call-site areas, relevant tests, and extraction risks before any code is moved.

Runtime behavior is not changed by this inventory. The repository names below describe the target ownership for later stages; they are not implemented APIs until their extraction stages are committed.

## Coverage check

- Public/non-private `Database` methods found in `src/db/database.py`: 117.
- Public methods assigned to exactly one owner below: 117.
- Private helpers such as `_table_columns()` and `_verify_stage2_surface_invariants()` are excluded from the method inventory and are owned by Stage 2 schema/migration extraction.

## Repository group summary

| Group | Target owner | Method count | Primary source call-site areas | Relevant tests | Extraction risk |
| --- | --- | ---: | --- | --- | --- |
| Database infrastructure | `Database infrastructure (no repository)` | 3 | `src/app.py`, `src/community_sync/edit_delete.py` | `tests/test_follow_subscription_flow.py`, `tests/test_phase8_edit_delete_sync.py`, `tests/test_user_identity_dump.py`, `tests/test_phase4_reply_preservation.py`, `tests/test_end_to_end_dedup_flow.py`, plus 23 more | Stage 2 must keep Database as the sole engine/session/create_all/migrate owner; do not move session ownership into repositories. |
| Legacy Lemmy mapping repository | `LegacyLemmyMappingRepository` | 12 | `src/discord_bot.py` | `tests/test_phase4_reply_preservation.py`, `tests/test_phase3_message_fanout_scenarios.py`, `tests/support/db.py` | Older remote Lemmy mapping paths are mixed with newer fanout code; extraction must preserve dedup keys and direct thread/message lookup behavior. |
| Event receipt repository | `EventReceiptRepository` | 3 | `src/http_api.py` | `tests/test_end_to_end_dedup_flow.py`, `tests/behavior/test_inbound_scenarios.py` | Inbound idempotency must keep the same delivery-id decisions and status transitions; repository updates persist status, outcome, and detail together. |
| Remote subscription repository | `RemoteSubscriptionRepository` | 9 | `src/discord_event_router.py`, `src/content_publish_service.py`, `src/operations/unsubscribe.py`, `src/local_communities/participant_routing.py`, `src/operations/subscribe_local_community.py`, plus 6 more | `tests/test_follow_subscription_flow.py`, `tests/test_federation_allowlist_handlers.py`, `tests/test_db_federation_identity.py`, `tests/behavior/test_unsubscribe_retry_scenarios.py`, `tests/behavior/test_subscription_scenarios.py`, plus 15 more | Subscribe/unsubscribe lifecycle and stale inbound filtering depend on exact subscription and follow-state rows. |
| User repository | `UserRepository` | 5 | `src/registration_service.py`, `src/http_api.py`, `src/content_sync/outbound_publish.py`, `src/operations/subscribe_local_community.py`, `src/operations/subscribe.py`, plus 2 more | `tests/test_phase4_reply_preservation.py`, `tests/test_phase3_message_fanout_scenarios.py`, `tests/test_phase6_dedup_hardening.py`, `tests/test_registration_flow.py`, `tests/test_user_identity_dump.py`, plus 11 more | Registration and actor-serving paths rely on uniqueness and actor URL lookups. |
| Registration session repository | `RegistrationSessionRepository` | 5 | `src/http_api.py` | `tests/test_registration_flow.py`, `tests/behavior/test_registration_scenarios.py`, `tests/behavior/test_cross_stage_scenarios.py` | OAuth/browser flow is stateful; extraction must keep token, OAuth state, and completion semantics unchanged. |
| Local community repository | `LocalCommunityRepository` | 6 | `src/operations/create_community.py`, `src/local_communities/service.py`, `src/operations/subscribe_local_community.py`, `src/local_communities/delivery_mapping.py`, `src/local_communities/participant_routing.py`, plus 5 more | `tests/test_stage5_schema_cleanup.py`, `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`, `tests/behavior/test_local_community_edit_delete_scenarios.py`, `tests/behavior/test_local_community_remote_fanout_scenarios.py`, `tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py`, plus 8 more | Local community identity is used by commands, actor rendering, dashboard, and runtime routing. |
| Remote subscriber repository | `RemoteSubscriberRepository` | 7 | `src/local_communities/runtime.py`, `src/local_communities/federation_fanout.py`, `src/dashboard.py` | `tests/behavior/test_dashboard_scenarios.py`, `tests/behavior/test_local_community_inbound_scenarios.py`, `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`, `tests/behavior/test_local_community_remote_fanout_scenarios.py`, `tests/behavior/test_local_subscriber_stage1_scenarios.py` | Undo(Follow), Accept(Follow), and fanout delivery source-of-truth behavior must stay unchanged. |
| Local subscriber repository | `LocalSubscriberRepository` | 7 | `src/operations/subscribe_local_community.py`, `src/operations/unsubscribe_local_community.py`, `src/local_communities/runtime.py`, `src/commands/unsubscribe.py`, `src/local_communities/participant_routing.py`, plus 3 more | `tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py`, `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`, `tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py`, `tests/behavior/test_local_community_surface_stage2_scenarios.py`, `tests/behavior/test_local_subscriber_stage1_scenarios.py`, plus 1 more | Same-instance subscriptions drive participant routing and local-source authority for edits/deletes. |
| Local community content repository | `LocalCommunityContentRepository` | 8 | `src/local_communities/runtime.py`, `src/local_communities/delivery_mapping.py`, `src/local_communities/discord_fanout.py`, `src/local_communities/reply_mapping.py` | `tests/test_stage5_schema_cleanup.py`, `tests/behavior/test_local_community_remote_fanout_scenarios.py`, `tests/behavior/test_local_community_edit_delete_scenarios.py`, `tests/behavior/test_local_community_inbound_scenarios.py`, `tests/behavior/test_local_community_surface_stage2_scenarios.py`, plus 3 more | Canonical AP object identity must stay separate from Discord surface rows. |
| Local community surface repository | `LocalCommunitySurfaceRepository` | 14 | `src/local_communities/runtime.py`, `src/local_communities/discord_fanout.py`, `src/local_communities/delivery_mapping.py`, `src/discord_event_router.py`, `src/local_communities/reply_mapping.py` | `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`, `tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py`, `tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py`, `tests/behavior/test_local_community_publish_scenarios.py`, `tests/behavior/test_local_community_surface_stage2_scenarios.py`, plus 1 more | Host vs local-subscriber surface roles are central to create retries and edit/delete authority. |
| Local community relay repository | `LocalCommunityRelayRepository` | 6 | `src/local_communities/federation_fanout.py` | `tests/behavior/test_local_community_remote_fanout_scenarios.py` | Relay retries depend on source/delivery uniqueness and current accepted remote-subscriber targeting. |
| Message mapping repository | `MessageMappingRepository` | 4 | `src/content_sync/persistence.py`, `src/local_communities/runtime.py`, `src/activitypub_handlers.py` | `tests/test_phase6_dedup_hardening.py`, `tests/test_end_to_end_dedup_flow.py`, `tests/test_db_federation_identity.py`, `tests/behavior/test_local_community_edit_delete_scenarios.py`, `tests/behavior/test_local_community_inbound_scenarios.py`, plus 5 more | Object/activity/source mapping IDs are federation compatibility boundaries and must not be regenerated or normalized differently. |
| ActivityPub object repository | `ActivityPubObjectRepository` | 3 | `src/content_sync/persistence.py`, `src/local_communities/runtime.py`, `src/content_sync/edit_delete.py` | `tests/test_db_federation_identity.py`, `tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py`, `tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py`, `tests/behavior/test_local_community_edit_delete_scenarios.py`, `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`, plus 3 more | Gateway object serving relies on persisted JSON and object/activity lookup compatibility. |
| Remote actor repository | `RemoteActorRepository` | 2 | None found by exact call-token search. | `tests/test_db_federation_identity.py` | Verification and delivery code depend on stable actor URL, inbox, shared inbox, and key cache semantics. |
| Discord fanout group repository | `DiscordFanoutGroupRepository` | 17 | `src/community_sync/runtime.py`, `src/community_sync/backfill.py`, `src/community_sync/delivery_mapping.py`, `src/activitypub_handlers.py`, `src/content_publish_service.py`, plus 3 more | `tests/test_phase8_edit_delete_sync.py`, `tests/test_phase9_bidirectional_mirror_messages.py`, `tests/test_phase6_dedup_hardening.py`, `tests/test_phase3_message_fanout_scenarios.py`, `tests/test_end_to_end_dedup_flow.py`, plus 10 more | Cross-channel fanout, reply preservation, and edit/delete lookup paths must keep the same dedup and delivery semantics. |
| Bridge actor follow repository | `BridgeActorFollowRepository` | 6 | `src/dashboard.py`, `src/activitypub_handlers.py`, `src/operations/unsubscribe.py`, `src/operations/subscribe.py` | `tests/test_follow_subscription_flow.py`, `tests/behavior/test_unsubscribed_inbound_activity_skip.py`, `tests/behavior/test_local_subscriber_stage1_scenarios.py`, `tests/behavior/test_unsubscribe_retry_scenarios.py`, `tests/behavior/test_subscription_scenarios.py`, plus 3 more | Remote follow acceptance now requires matching BridgeActorFollow; extraction must not reintroduce direct-follow compatibility. |


## Management action transaction boundary

Management command mutations that require audit rows no longer live as `*_with_audit` repository variants. `src/management_actions.py` owns the application-service transaction boundary for local-community creation, community settings changes, ban activation/reactivation, and ban removal. Domain repositories remain persistence-only and expose session-aware helpers for that service. `src/management_audit_recorder.py` owns audit-row target/action/reason semantics and delegates low-level inserts to `ManagementAuditEventRepository`.

## Method inventory by target owner

### Database infrastructure

Target owner: `Database infrastructure (no repository)`.

| Current `Database` method | Primary source call sites | Relevant tests |
| --- | --- | --- |
| `create_all` | `src/app.py` | `tests/test_follow_subscription_flow.py`, `tests/test_phase8_edit_delete_sync.py`, `tests/test_user_identity_dump.py`, `tests/test_db_federation_identity.py`, `tests/test_phase4_reply_preservation.py`, `tests/test_federation_allowlist_handlers.py`, plus 17 more |
| `migrate` | `src/app.py` | `tests/test_stage5_schema_cleanup.py`, `tests/behavior/test_local_community_surface_stage2_scenarios.py` |
| `session` | `src/community_sync/edit_delete.py` | `tests/test_phase5_inbound_ap_shared_groups.py`, `tests/behavior/test_local_community_publish_scenarios.py`, `tests/behavior/test_publish_scenarios.py`, `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`, `tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py` |

### Legacy Lemmy mapping repository

Target owner: `LegacyLemmyMappingRepository`.

| Current `Database` method | Primary source call sites | Relevant tests |
| --- | --- | --- |
| `get_post_link_by_thread_id` | `src/discord_bot.py` | None found by exact call-token search. |
| `get_post_link_by_lemmy_post_id` | None found by exact call-token search. | None found by exact call-token search. |
| `get_post_link_by_lemmy_post_ap_id` | None found by exact call-token search. | None found by exact call-token search. |
| `get_post_links_by_lemmy_post_ap_id` | None found by exact call-token search. | None found by exact call-token search. |
| `get_post_link_by_lemmy_post_ap_id_and_channel_id` | None found by exact call-token search. | None found by exact call-token search. |
| `create_post_link` | None found by exact call-token search. | `tests/test_phase3_message_fanout_scenarios.py`, `tests/test_phase4_reply_preservation.py`, `tests/support/db.py` |
| `has_comment_link_for_discord_message` | None found by exact call-token search. | None found by exact call-token search. |
| `has_comment_link_for_lemmy_comment` | None found by exact call-token search. | None found by exact call-token search. |
| `get_comment_link_by_lemmy_comment_ap_id` | None found by exact call-token search. | None found by exact call-token search. |
| `get_comment_links_by_lemmy_comment_ap_id` | None found by exact call-token search. | None found by exact call-token search. |
| `get_comment_link_by_discord_message_id` | None found by exact call-token search. | None found by exact call-token search. |
| `create_comment_link` | None found by exact call-token search. | None found by exact call-token search. |

### Event receipt repository

Target owner: `EventReceiptRepository`. Receipt writes keep retry/idempotency `status`, semantic `outcome`, and human `detail` in one transaction; retry starts clear stale outcomes.

| Current `Database` method | Primary source call sites | Relevant tests |
| --- | --- | --- |
| `get_event_receipt` | `src/http_api.py` | `tests/test_end_to_end_dedup_flow.py`, `tests/behavior/test_inbound_scenarios.py` |
| `create_event_receipt` | `src/http_api.py` | None found by exact call-token search. |
| `update_event_receipt` | `src/http_api.py` | None found by exact call-token search. |

### Remote subscription repository

Target owner: `RemoteSubscriptionRepository`.

| Current `Database` method | Primary source call sites | Relevant tests |
| --- | --- | --- |
| `get_subscription_by_channel` | `src/commands/unsubscribe.py`, `src/discord_event_router.py`, `src/operations/unsubscribe.py`, `src/community_sync/runtime.py`, `src/operations/subscribe_local_community.py`, `src/content_publish_service.py`, plus 2 more | `tests/test_federation_allowlist_handlers.py`, `tests/test_db_federation_identity.py`, `tests/test_follow_subscription_flow.py`, `tests/behavior/test_subscription_scenarios.py`, `tests/behavior/test_unsubscribe_retry_scenarios.py`, `tests/behavior/test_unified_community_discovery_scenarios.py`, plus 1 more |
| `get_subscriptions_by_community` | `src/community_sync/inbound_mapping.py`, `src/community_sync/runtime.py` | `tests/behavior/test_unsubscribed_inbound_activity_skip.py`, `tests/test_follow_subscription_flow.py` |
| `get_all_subscriptions` | `src/operations/list_subscriptions.py` | None found by exact call-token search. |
| `get_subscriptions_by_guild` | `src/operations/list_subscriptions.py` | None found by exact call-token search. |
| `create_subscription` | `src/operations/subscribe.py` | `tests/test_follow_subscription_flow.py`, `tests/test_phase9_bidirectional_mirror_messages.py`, `tests/test_phase6_dedup_hardening.py`, `tests/test_phase4_reply_preservation.py`, `tests/test_federation_allowlist_handlers.py`, `tests/test_phase3_message_fanout_scenarios.py`, plus 12 more |
| `update_subscription_follow_state` | None found by exact call-token search. | `tests/test_db_federation_identity.py` |
| `delete_subscription` | `src/operations/subscribe.py`, `src/operations/unsubscribe.py` | None found by exact call-token search. |
| `count_subscriptions_for_community` | `src/operations/unsubscribe.py` | None found by exact call-token search. |
| `get_pending_channel_subscriptions_for_community` | `src/activitypub_handlers.py` | None found by exact call-token search. |

### User repository

Target owner: `UserRepository`.

| Current `Database` method | Primary source call sites | Relevant tests |
| --- | --- | --- |
| `create_user` | `src/registration_service.py` | `tests/test_phase9_bidirectional_mirror_messages.py`, `tests/test_user_identity_dump.py`, `tests/test_registration_flow.py`, `tests/test_phase6_dedup_hardening.py`, `tests/test_phase5_inbound_ap_shared_groups.py`, `tests/test_phase4_reply_preservation.py`, plus 10 more |
| `get_user_by_discord_user_id` | `src/registration_service.py`, `src/http_api.py`, `src/operations/subscribe_local_community.py`, `src/content_sync/outbound_publish.py`, `src/operations/subscribe.py` | `tests/test_registration_flow.py`, `tests/test_db_federation_identity.py`, `tests/behavior/test_registration_scenarios.py` |
| `get_user_by_activitypub_username` | `src/http_api.py`, `src/registration_service.py` | `tests/test_registration_flow.py`, `tests/test_db_federation_identity.py`, `tests/behavior/test_registration_scenarios.py` |
| `get_user_by_actor_url` | `src/activitypub_handlers.py` | `tests/test_db_federation_identity.py` |
| `list_users` | `src/dashboard.py` | None found by exact call-token search. |

### Registration session repository

Target owner: `RegistrationSessionRepository`.

| Current `Database` method | Primary source call sites | Relevant tests |
| --- | --- | --- |
| `create_registration_session` | `src/http_api.py` | None found by exact call-token search. |
| `get_registration_session_by_token` | `src/http_api.py` | `tests/test_registration_flow.py`, `tests/behavior/test_registration_scenarios.py`, `tests/behavior/test_cross_stage_scenarios.py` |
| `update_registration_session_oauth_state` | `src/http_api.py` | None found by exact call-token search. |
| `update_registration_session_discord_identity` | `src/http_api.py` | None found by exact call-token search. |
| `mark_registration_session_completed` | `src/http_api.py` | None found by exact call-token search. |

### Local community repository

Target owner: `LocalCommunityRepository`.

| Current `Database` method | Primary source call sites | Relevant tests |
| --- | --- | --- |
| `create_local_community` | `src/operations/create_community.py`, `src/local_communities/service.py` | `tests/test_stage5_schema_cleanup.py`, `tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py`, `tests/behavior/test_dashboard_scenarios.py`, `tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py`, `tests/behavior/test_local_subscriber_stage1_scenarios.py`, `tests/behavior/test_local_community_surface_stage2_scenarios.py`, plus 7 more |
| `get_local_community_by_forum_channel_id` | `src/local_communities/service.py`, `src/local_communities/delivery_mapping.py`, `src/operations/subscribe_local_community.py`, `src/local_communities/participant_routing.py` | None found by exact call-token search. |
| `get_local_community_by_actor_url` | `src/local_communities/inbound_mapping.py`, `src/local_communities/runtime.py` | None found by exact call-token search. |
| `get_local_community_by_slug` | `src/local_communities/service.py` | `tests/test_stage5_schema_cleanup.py`, `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`, `tests/behavior/test_local_community_edit_delete_scenarios.py`, `tests/behavior/test_local_community_surface_stage2_scenarios.py`, `tests/behavior/test_dashboard_scenarios.py`, `tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py`, plus 6 more |
| `get_local_community_by_id` | `src/operations/unsubscribe_local_community.py`, `src/commands/list_subs.py`, `src/operations/subscribe_local_community.py`, `src/local_communities/participant_routing.py` | None found by exact call-token search. |
| `list_local_communities` | `src/dashboard.py` | None found by exact call-token search. |

### Remote subscriber repository

Target owner: `RemoteSubscriberRepository`.

| Current `Database` method | Primary source call sites | Relevant tests |
| --- | --- | --- |
| `create_remote_subscriber` | `src/local_communities/runtime.py` | `tests/behavior/test_local_community_inbound_scenarios.py`, `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`, `tests/behavior/test_local_community_remote_fanout_scenarios.py`, `tests/behavior/test_dashboard_scenarios.py` |
| `get_remote_subscriber` | `src/local_communities/runtime.py` | `tests/behavior/test_local_community_inbound_scenarios.py`, `tests/behavior/test_local_subscriber_stage1_scenarios.py` |
| `get_remote_subscriber_by_follow_activity_id` | None found by exact call-token search. | None found by exact call-token search. |
| `update_remote_subscriber_acceptance` | `src/local_communities/runtime.py` | None found by exact call-token search. |
| `delete_remote_subscriber` | `src/local_communities/runtime.py` | `tests/behavior/test_local_community_remote_fanout_scenarios.py` |
| `list_remote_subscribers` | `src/local_communities/federation_fanout.py` | `tests/behavior/test_local_community_inbound_scenarios.py` |
| `list_remote_subscribers_for_all` | `src/dashboard.py` | None found by exact call-token search. |

### Local subscriber repository

Target owner: `LocalSubscriberRepository`.

| Current `Database` method | Primary source call sites | Relevant tests |
| --- | --- | --- |
| `create_local_subscriber` | `src/operations/subscribe_local_community.py` | `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`, `tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py`, `tests/behavior/test_local_community_surface_stage2_scenarios.py`, `tests/behavior/test_local_subscriber_stage1_scenarios.py`, `tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py` |
| `get_local_subscriber` | `src/operations/unsubscribe_local_community.py`, `src/local_communities/runtime.py` | `tests/behavior/test_unified_community_discovery_scenarios.py` |
| `get_local_subscriber_by_channel` | `src/commands/unsubscribe.py`, `src/operations/unsubscribe_local_community.py`, `src/local_communities/participant_routing.py`, `src/operations/subscribe_local_community.py` | `tests/behavior/test_local_subscriber_stage1_scenarios.py`, `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py` |
| `list_local_subscribers` | `src/local_communities/discord_fanout.py` | None found by exact call-token search. |
| `list_local_subscribers_by_guild` | `src/operations/list_subscriptions.py` | None found by exact call-token search. |
| `delete_local_subscriber` | `src/operations/unsubscribe_local_community.py` | `tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py` |
| `count_local_subscribers` | `src/dashboard.py` | None found by exact call-token search. |

### Local community content repository

Target owner: `LocalCommunityContentRepository`.

| Current `Database` method | Primary source call sites | Relevant tests |
| --- | --- | --- |
| `create_local_community_thread` | `src/local_communities/runtime.py` | `tests/test_stage5_schema_cleanup.py`, `tests/behavior/test_local_community_edit_delete_scenarios.py`, `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`, `tests/behavior/test_local_community_surface_stage2_scenarios.py`, `tests/behavior/test_local_community_inbound_scenarios.py`, `tests/behavior/test_local_community_remote_fanout_scenarios.py` |
| `create_local_community_thread_canonical` | `src/local_communities/runtime.py` | `tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py`, `tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py` |
| `get_local_community_thread_by_ap_object_id` | `src/local_communities/runtime.py`, `src/local_communities/delivery_mapping.py` | `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`, `tests/behavior/test_local_community_inbound_scenarios.py`, `tests/behavior/test_local_community_surface_stage2_scenarios.py`, `tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py` |
| `create_local_community_message` | `src/local_communities/runtime.py` | `tests/test_stage5_schema_cleanup.py`, `tests/behavior/test_local_community_inbound_scenarios.py`, `tests/behavior/test_local_community_edit_delete_scenarios.py`, `tests/behavior/test_local_community_surface_stage2_scenarios.py` |
| `create_local_community_message_canonical` | `src/local_communities/runtime.py` | `tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py`, `tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py` |
| `get_local_community_message_by_ap_object_id` | `src/local_communities/runtime.py`, `src/local_communities/reply_mapping.py`, `src/local_communities/discord_fanout.py`, `src/local_communities/delivery_mapping.py` | `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`, `tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py`, `tests/behavior/test_local_community_surface_stage2_scenarios.py`, `tests/behavior/test_local_community_inbound_scenarios.py` |
| `list_local_community_messages_for_thread` | None found by exact call-token search. | None found by exact call-token search. |
| `get_local_community_thread_by_id` | `src/local_communities/runtime.py` | None found by exact call-token search. |

### Local community surface repository

Target owner: `LocalCommunitySurfaceRepository`.

| Current `Database` method | Primary source call sites | Relevant tests |
| --- | --- | --- |
| `create_local_community_thread_surface` | `src/local_communities/discord_fanout.py`, `src/local_communities/runtime.py` | `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`, `tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py`, `tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py` |
| `get_local_community_thread_surface` | `src/local_communities/discord_fanout.py` | `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`, `tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py`, `tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py` |
| `get_local_community_thread_surface_by_discord_thread_id` | `src/local_communities/delivery_mapping.py` | `tests/behavior/test_local_community_publish_scenarios.py`, `tests/behavior/test_local_community_surface_stage2_scenarios.py` |
| `get_local_community_thread_surface_by_starter_message_id` | `src/discord_event_router.py`, `src/local_communities/runtime.py` | None found by exact call-token search. |
| `list_local_community_thread_surfaces` | `src/local_communities/discord_fanout.py` | `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`, `tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py`, `tests/behavior/test_local_community_surface_stage2_scenarios.py` |
| `get_host_local_community_thread_surface` | `src/local_communities/runtime.py`, `src/local_communities/reply_mapping.py` | None found by exact call-token search. |
| `create_local_community_message_surface` | `src/local_communities/runtime.py`, `src/local_communities/discord_fanout.py` | `tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py`, `tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py` |
| `get_local_community_message_surface` | `src/local_communities/discord_fanout.py` | `tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py` |
| `get_local_community_message_surface_by_discord_message_id` | `src/discord_event_router.py`, `src/local_communities/reply_mapping.py`, `src/local_communities/delivery_mapping.py`, `src/local_communities/runtime.py` | `tests/behavior/test_local_community_publish_scenarios.py`, `tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py`, `tests/behavior/test_local_community_surface_stage2_scenarios.py`, `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py` |
| `list_local_community_message_surfaces` | `src/local_communities/discord_fanout.py` | `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`, `tests/behavior/test_local_community_surface_stage2_scenarios.py` |
| `get_host_local_community_message_surface` | `src/local_communities/reply_mapping.py` | `tests/behavior/test_local_community_inbound_scenarios.py` |
| `get_local_community_thread_surface_by_id` | `src/local_communities/discord_fanout.py` | None found by exact call-token search. |
| `get_local_community_thread_for_surface` | `src/local_communities/runtime.py`, `src/local_communities/delivery_mapping.py` | `tests/behavior/test_local_community_publish_scenarios.py` |
| `get_local_community_message_for_surface` | `src/local_communities/reply_mapping.py`, `src/local_communities/delivery_mapping.py`, `src/local_communities/runtime.py` | `tests/behavior/test_local_community_publish_scenarios.py` |

### Local community relay repository

Target owner: `LocalCommunityRelayRepository`.

| Current `Database` method | Primary source call sites | Relevant tests |
| --- | --- | --- |
| `get_or_create_local_community_relay_source_activity` | `src/local_communities/federation_fanout.py` | None found by exact call-token search. |
| `list_local_community_relay_deliveries_for_source` | None found by exact call-token search. | None found by exact call-token search. |
| `create_missing_local_community_relay_deliveries` | `src/local_communities/federation_fanout.py` | None found by exact call-token search. |
| `list_delivered_local_community_create_relay_targets` | `src/local_communities/federation_fanout.py` | `tests/behavior/test_local_community_remote_fanout_scenarios.py` |
| `mark_local_community_relay_delivery_result` | `src/local_communities/federation_fanout.py` | None found by exact call-token search. |
| `get_local_community_relay_source_activity` | None found by exact call-token search. | None found by exact call-token search. |

### Message mapping repository

Target owner: `MessageMappingRepository`.

| Current `Database` method | Primary source call sites | Relevant tests |
| --- | --- | --- |
| `create_message_mapping` | `src/content_sync/persistence.py`, `src/local_communities/runtime.py` | `tests/test_db_federation_identity.py`, `tests/test_end_to_end_dedup_flow.py`, `tests/test_phase6_dedup_hardening.py`, `tests/behavior/test_inbound_scenarios.py`, `tests/behavior/test_local_community_inbound_scenarios.py`, `tests/behavior/test_local_community_edit_delete_scenarios.py` |
| `get_message_mapping_by_activity_id` | `src/activitypub_handlers.py`, `src/local_communities/runtime.py` | `tests/test_db_federation_identity.py` |
| `get_message_mapping_by_object_id` | `src/activitypub_handlers.py`, `src/local_communities/runtime.py` | `tests/test_db_federation_identity.py`, `tests/behavior/test_local_community_inbound_scenarios.py` |
| `get_message_mapping_by_discord_message_id` | None found by exact call-token search. | `tests/test_community_runtime_scenarios.py`, `tests/test_discord_publish_flow.py`, `tests/test_db_federation_identity.py`, `tests/behavior/test_cross_stage_scenarios.py`, `tests/behavior/test_publish_scenarios.py` |

### ActivityPub object repository

Target owner: `ActivityPubObjectRepository`.

| Current `Database` method | Primary source call sites | Relevant tests |
| --- | --- | --- |
| `create_published_activity_object` | `src/content_sync/persistence.py` | `tests/test_db_federation_identity.py`, `tests/behavior/test_local_community_edit_delete_scenarios.py`, `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`, `tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py`, `tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py` |
| `get_published_activity_object_by_object_id` | `src/local_communities/runtime.py` | `tests/test_community_runtime_scenarios.py`, `tests/test_discord_publish_flow.py`, `tests/test_db_federation_identity.py`, `tests/behavior/test_publish_scenarios.py` |
| `get_published_activity_object_by_discord_message_id` | `src/content_sync/edit_delete.py` | None found by exact call-token search. |

### Remote actor repository

Target owner: `RemoteActorRepository`.

| Current `Database` method | Primary source call sites | Relevant tests |
| --- | --- | --- |
| `upsert_remote_actor` | None found by exact call-token search. | `tests/test_db_federation_identity.py` |
| `get_remote_actor_by_actor_url` | None found by exact call-token search. | `tests/test_db_federation_identity.py` |

### Discord fanout group repository

Target owner: `DiscordFanoutGroupRepository`.

| Current `Database` method | Primary source call sites | Relevant tests |
| --- | --- | --- |
| `create_thread_group` | `src/community_sync/runtime.py`, `src/community_sync/backfill.py` | `tests/test_community_runtime_scenarios.py`, `tests/test_phase6_dedup_hardening.py`, `tests/test_discord_publish_flow.py`, `tests/test_phase3_message_fanout_scenarios.py`, `tests/test_phase9_bidirectional_mirror_messages.py`, `tests/test_phase5_inbound_ap_shared_groups.py`, plus 8 more |
| `get_thread_group_by_source_thread` | `src/community_sync/delivery_mapping.py` | `tests/test_community_runtime_scenarios.py`, `tests/test_phase2_fanout_scenarios.py` |
| `get_thread_group_by_ap_object` | `src/community_sync/backfill.py`, `src/activitypub_handlers.py`, `src/community_sync/delivery_mapping.py` | `tests/test_community_runtime_scenarios.py`, `tests/test_phase6_dedup_hardening.py`, `tests/test_phase5_inbound_ap_shared_groups.py`, `tests/behavior/test_unsubscribed_inbound_activity_skip.py`, `tests/behavior/test_inbound_comment_backfill.py`, `tests/behavior/test_inbound_scenarios.py` |
| `get_thread_group_by_id` | `src/community_sync/runtime.py` | None found by exact call-token search. |
| `get_thread_group_by_any_thread` | `src/content_publish_service.py`, `src/community_sync/delivery_mapping.py` | None found by exact call-token search. |
| `add_thread_delivery` | `src/community_sync/runtime.py`, `src/community_sync/backfill.py` | `tests/test_community_runtime_scenarios.py`, `tests/test_phase8_edit_delete_sync.py`, `tests/test_discord_publish_flow.py`, `tests/test_phase6_dedup_hardening.py`, `tests/test_end_to_end_dedup_flow.py`, `tests/test_phase9_bidirectional_mirror_messages.py`, plus 7 more |
| `get_thread_deliveries` | `src/content_publish_service.py`, `src/community_sync/runtime.py`, `src/community_sync/reply_mapping.py`, `src/community_sync/inbound_mapping.py`, `src/community_sync/backfill.py`, `src/community_sync/delivery_mapping.py` | `tests/test_phase6_dedup_hardening.py`, `tests/test_phase2_fanout_scenarios.py`, `tests/test_phase5_inbound_ap_shared_groups.py`, `tests/behavior/test_inbound_comment_backfill.py`, `tests/behavior/test_inbound_scenarios.py` |
| `get_thread_delivery_by_thread` | `src/discord_bot.py` | `tests/test_phase6_dedup_hardening.py`, `tests/test_phase9_bidirectional_mirror_messages.py` |
| `create_message_group` | `src/community_sync/runtime.py` | `tests/test_discord_publish_flow.py`, `tests/test_phase5_inbound_ap_shared_groups.py`, `tests/test_phase3_message_fanout_scenarios.py`, `tests/test_phase4_reply_preservation.py`, `tests/test_phase8_edit_delete_sync.py`, `tests/test_phase9_bidirectional_mirror_messages.py`, plus 2 more |
| `get_message_group_by_id` | `src/community_sync/runtime.py` | None found by exact call-token search. |
| `get_message_group_by_source_message` | `src/community_sync/delivery_mapping.py` | `tests/test_community_runtime_scenarios.py`, `tests/test_phase3_message_fanout_scenarios.py`, `tests/test_phase5_inbound_ap_shared_groups.py`, `tests/test_phase4_reply_preservation.py` |
| `get_message_group_by_ap_object` | `src/activitypub_handlers.py`, `src/community_sync/delivery_mapping.py`, `src/community_sync/inbound_mapping.py` | `tests/test_phase5_inbound_ap_shared_groups.py`, `tests/test_end_to_end_dedup_flow.py`, `tests/behavior/test_unsubscribed_inbound_activity_skip.py`, `tests/test_community_runtime_scenarios.py`, `tests/behavior/test_inbound_scenarios.py`, `tests/behavior/test_inbound_comment_backfill.py` |
| `get_message_group_by_delivered_message` | `src/content_publish_service.py`, `src/community_sync/reply_mapping.py`, `src/community_sync/delivery_mapping.py` | None found by exact call-token search. |
| `add_message_delivery` | `src/community_sync/runtime.py` | `tests/test_phase6_dedup_hardening.py`, `tests/test_phase5_inbound_ap_shared_groups.py`, `tests/test_discord_publish_flow.py`, `tests/test_phase9_bidirectional_mirror_messages.py`, `tests/test_phase4_reply_preservation.py`, `tests/test_phase8_edit_delete_sync.py`, plus 1 more |
| `get_message_delivery_by_message` | `src/discord_bot.py` | None found by exact call-token search. |
| `get_message_deliveries` | `src/community_sync/runtime.py` | `tests/test_phase5_inbound_ap_shared_groups.py`, `tests/test_phase3_message_fanout_scenarios.py`, `tests/test_phase4_reply_preservation.py`, `tests/behavior/test_inbound_scenarios.py`, `tests/behavior/test_inbound_comment_backfill.py` |
| `get_message_delivery_in_thread` | `src/community_sync/reply_mapping.py`, `src/community_sync/delivery_mapping.py` | None found by exact call-token search. |

### Bridge actor follow repository

Target owner: `BridgeActorFollowRepository`.

| Current `Database` method | Primary source call sites | Relevant tests |
| --- | --- | --- |
| `list_bridge_actor_follows` | `src/dashboard.py` | None found by exact call-token search. |
| `get_bridge_actor_follow` | `src/activitypub_handlers.py`, `src/operations/unsubscribe.py`, `src/operations/subscribe.py` | `tests/test_follow_subscription_flow.py`, `tests/behavior/test_unsubscribed_inbound_activity_skip.py`, `tests/behavior/test_unsubscribe_retry_scenarios.py`, `tests/behavior/test_local_subscriber_stage1_scenarios.py`, `tests/behavior/test_unified_community_discovery_scenarios.py`, `tests/behavior/test_subscription_scenarios.py` |
| `get_bridge_actor_follow_by_follow_activity_id` | `src/activitypub_handlers.py` | None found by exact call-token search. |
| `create_bridge_actor_follow` | `src/operations/subscribe.py` | `tests/test_federation_allowlist_handlers.py`, `tests/test_follow_subscription_flow.py`, `tests/behavior/test_unsubscribed_inbound_activity_skip.py`, `tests/behavior/test_dashboard_scenarios.py`, `tests/behavior/test_unsubscribe_retry_scenarios.py`, `tests/behavior/test_subscription_scenarios.py` |
| `mark_bridge_actor_follow_accepted` | `src/activitypub_handlers.py` | None found by exact call-token search. |
| `delete_bridge_actor_follow` | `src/operations/unsubscribe.py`, `src/operations/subscribe.py` | None found by exact call-token search. |

## Stage-order notes

- Stage 1 can use this inventory and the existing table map to add or verify navigation banners without moving code.
- Stage 2 owns infrastructure helpers and private schema/migration helpers, including `_table_columns()` and `_verify_stage2_surface_invariants()`.
- Stage 3 owns the local-community repository groups: local communities, remote subscribers, local subscribers, local-community content, local-community surfaces, and local-community relay.
- Stage 4 owns remote subscriptions and bridge actor follows.
- Stage 5 owns users, registration sessions, and event receipts.
- Stage 6 owns message mappings, published ActivityPub objects, and remote actors.
- Stage 7 owns legacy Lemmy mappings and Discord fanout groups.
- Stage 8 removes temporary facade wrappers after call sites use repository properties directly.

## Blind spots for later stages

- Exact call-token search can miss dynamically dispatched calls or direct ORM access through `Database.session()`. Later stages must inspect the relevant runtime modules manually before moving code.
- Some tests create rows through helper functions in `tests/support/`; extraction stages should treat those as compatibility call sites, not only assertions.
- `Database.session()` is used directly in several tests and runtime edge paths; Stage 2 must preserve it as an infrastructure facade rather than treating it as a domain repository method.
- Methods without exact Stage 0 source call sites were treated as compatibility call sites during extraction; after Stage 8, repository properties are the supported domain API.

## Final repository API status

Stage 8 removed the temporary `Database.*` domain forwarding wrappers. `Database` now owns only engine/session/schema lifecycle plus repository construction; domain persistence operations are supported through repository properties such as `database.local_communities`, `database.remote_subscriptions`, `database.activitypub_objects`, and `database.discord_fanout_groups`.

This inventory remains a migration/history map from the former facade methods to the final repository owners. It should not be read as a supported `Database.method(...)` API list after Stage 8.


## Cross-table Discord forum placement

`src/discord_forum_placement.py` is the command-layer reader for channel exclusivity across `local_communities`, `channel_community_subscriptions`, and `local_subscribers`. It does not own schema or persistence; it calls repository methods to reject occupied selected channels and to decide whether a bot-created channel is safe to delete after a later command failure.
