# Test coverage map

This document is a short, feature-oriented index of the test suite. It helps maintainers find existing coverage before adding a new test. It intentionally links to files rather than line numbers or individual test functions, because those change more often.

## Stage 04 S-01 duplicate Community Create audit

- `docs/development/stage04_s01_legacy_create_audit.md` inventories the
  normalized production call graph, 14 test files with 62 direct raw runtime
  invocations, external/dynamic compatibility risk and staged deletion gate.
- `tests/support/community_events.py` supplies observed typed source events and
  physical boundary fakes for the final raw-consumer migration.
- `tests/test_stage04_single_community_create_route_contract.py` rejects a
  reintroduced raw `DiscordEventRouter` Create entry; the migrated router cases
  in three Local Community behavior modules verify Local/Remote selection,
  inactive subscribers and exact forum reply ownership through typed events.
- S-01 follow-up converts `tests/test_phase2_fanout_scenarios.py`,
  `tests/test_phase3_message_fanout_scenarios.py`,
  `tests/test_phase4_reply_preservation.py`,
  `tests/test_phase9_bidirectional_mirror_messages.py`, the outbound case in
  `tests/test_phase5_inbound_ap_shared_groups.py`, and both authored cases in
  `tests/test_community_runtime_scenarios.py` to actual
  `handle_normalized_thread_create` / `handle_normalized_message_create` with
  immutable SQLite physical receipts and a render-only gateway. The two inbound
  cases in `test_community_runtime_scenarios.py` now bind a typed adapter too.
  `test_normalized_community_runtime.py` protects root replies with no explicit
  Discord reference: canonical persistence owns the source receipt and the
  runtime must not write a contradictory second row after AP commit.
- **Remaining S-01 migration:** 32 raw runtime Create calls in 8 behavior/
  migration suites still need case-by-case conversion before deleting the four
  SDK-shaped runtime entry points. Do not remove those tests or insert a raw
  compatibility shim to satisfy method existence.
- `tests/test_normalized_community_runtime.py` and
  `tests/test_normalized_local_community_runtime.py` are the canonical SQLite
  sources for fully normalized Create/dedup/fanout behavior. Legacy raw runtime
  suites must be migrated before their direct SDK-shaped methods are removed.

## Stage 04 durable Create admission and recovery (Checkpoints 56–60)

- Current-v1 bootstrap and released-v0→v1 migration preservation, including
  nullable legacy mutation baselines and exact source/surface identities —
  `tests/test_v0_to_v1_message_fanout_schema.py`.
- Local/Remote AP Create canonical-before-physical-POST, duplicate AP source,
  content snapshot, orphan/unknown transport, correct recipient and exact
  nested parent/wake-up — `tests/test_stage04_delivery_intents.py`,
  `tests/test_stage04_local_inbound_admission.py`,
  `tests/test_stage04_parent_mapping.py`,
  `tests/test_local_inbound_adapter_delivery.py`,
  `tests/test_normalized_inbound_remote_posts.py` and
  `tests/test_normalized_inbound_remote_comments.py`.
- No raw Discord SDK fallback without a bound normalized adapter, no
  unverified Local Community lifecycle after reservation, current typed
  router source selection and SDK-ready recovery on the existing worker —
  `tests/test_stage04_no_sdk_ap_fallback.py`,
  `tests/test_stage04_physical_recovery_schedule.py`.

These are focused tests and do not substitute for full Python/Node or live
Discord integration tests, which require the project dependencies.

### Stage 04 post-review physical receipt handoff

- SQLite domain fan-out after coordinator-created Local Community threads with
  `NULL` historical Discord ID projections: verified distinct A/C root replies,
  durable nested `blocked` intent for a missing destination parent and wake
  after the exact parent receipt; unknown parent has zero new sends/intents —
  `tests/behavior/test_stage04_local_receipt_handoff.py`.
- Real Local Community inbound AP post then comment through the runtime,
  including host thread with typed-only receipt and idempotent replay;
  mutation render API rejects untyped direct inbox URL parameters — same file.

### Stage 04 Remote Community typed receipt handoff (review P1)

- Real SQLite and domain handlers: new B/C coordinator-created threads with
  NULL legacy Discord projections admit later source comments, replies
  originating in mirrored threads, inbound authenticated AP comments, and
  immutable target-local parent references. A wrong forum/thread claim is
  rejected; a known nested parent absent from one forum creates `blocked`
  without sending a flat reply —
  `tests/test_stage04_remote_typed_receipt_regressions.py`.
- Normalized Remote Community ingress and direct sibling fan-out fixtures use
  verified `MessageView.created_at`, side-effect-free `render_community`, and
  persisted canonical source post/comment IDs before coordinator dispatch.
  Regressions assert exact `channel_surface_id` and `external_*` receipt fields,
  current recipient authorization, immutable reply parents, wrong-mode receipt
  refusal, no AP activity on missing source timestamps or renderer ID drift,
  and no unexpected network sends — `tests/test_normalized_community_runtime.py`,
  `tests/test_neutral_sibling_fanout.py`. New coordinator-created copies do not
  need the nullable legacy `discord_*` projections.
- Normalized reply-only tests seed real accepted source/target `ChannelSurface`
  claims rather than unattached legacy copy rows, preserving strict foreign
  connector/container rejection and correct sibling parent references —
  `tests/test_neutral_reply_resolution.py`.

### Stage 04 post-review Local/Remote legacy scenario contract refresh

- Real Local Community source post/comment tests retain mandatory verified starter
  `MessageView.created_at`, deterministic render-only AP identity, and a
  full `PlatformAdapterPort` fake; echoes from bot starters cannot republish
  canonical content — `tests/test_normalized_local_community_runtime.py`,
  `tests/test_neutral_local_community_fanout.py`.
- Local/Remote inbound and backfill regression tests assert connector-scoped
  `channel_surface_id` and `external_*` receipts, not nullable historic
  `discord_*` projections. Host rebind tests release the existing typed claim
  without mutating the immutable channel ID —
  `tests/test_local_inbound_adapter_delivery.py`,
  `tests/test_normalized_inbound_remote_posts.py`,
  `tests/test_normalized_inbound_remote_comments.py`,
  `tests/test_normalized_ap_backfill.py`.
- Inbound Local Community mutation tests seed authenticated source content,
  verified independent Update version and source activity ID; exercise the
  real durable physical mutation worker and reject missing clock/activity or
  forged actor before any physical edit — `tests/test_local_inbound_mutations.py`.

## Stage 04 typed Community source mutations (Checkpoint 61 WIP)

- Real SQLite source post/comment + current surface and author validation,
  two distinct immediate thread containers, partial refetch authorization,
  scoped durable reconciliation, owner rebinding before writer admission and
  before deferred read, ID-only delete, no forged Delete on partial-read 404,
  and separate legacy Personal Feed delivery —
  `tests/test_stage04_typed_community_mutation_ingress.py`.
- These tests exercise the admission/repository boundary, not end-to-end
  Discord SDK or the complete authored mutation transport and restart matrix.

## Stage 04 authored Group Announce claim/restart (Checkpoints 61–63)

- Real SQLite per-object/exact-Follow Create acknowledgment before Update,
  Delete terminal fence, original-attempt late acknowledgment, unknown send
  after lease expiry, and zero claim capacity —
  `tests/test_stage04_authored_announces.py`.
- Graceful cancellation during the first Group HTTP send leaves the in-flight
  exact Follow attempt uncertain but releases the other, never-sent recipient
  claims for a safe retry — `tests/test_stage04_authored_announces.py`.
- A stale `dispatching` lease transitions to persistent `ambiguous`; only a
  verified positive result from that same token/generation can settle it.
  Older unknown Update must block a newer Delete after restart.

## Stage 04 physical mutation recovery coverage (Checkpoints 61–63)

- Real SQLite canonical Update/Delete against historical bot-owned copies,
  unknown outcomes, expired dispatch, revoked surfaces, and physical starter
  tombstones — `tests/test_stage04_physical_mutation_recovery.py`.
- Recovery scanner starvation: a copy beyond eight already-acknowledged rows
  must still receive its authorized revision, while revoked destinations stay
  blocked, `limit=1` sends only one claim, and `limit=0` performs no sends —
  `tests/test_stage04_physical_mutation_recovery.py`.
- A malformed canonical JSON or domain renderer refusal leaves its one copy
  unacknowledged and diagnostic, does not block a healthy sibling, and a later
  verified revision can recover it — `tests/test_stage04_physical_mutation_recovery.py`.
- Pre-transport adapter/authorization/formatter failure versus uncertain
  provider result, and cancellation of a multi-copy claim batch after one
  adapter call (only unstarted copies become retryable) —
  `tests/test_stage04_physical_mutation_recovery.py`.
- Multi-forum nested parent isolation and restart error preservation —
  `tests/behavior/test_platform_message_fanout_scenarios.py` and
  `tests/behavior/test_platform_delivery_recovery_scenarios.py`.

## Test layers

- **Behavior** — user or platform action in a defined system state, verified through observable runtime effects.
- **Operation** — command business rules, precondition ordering, state changes, and audit boundaries.
- **Command** — Discord adapter behavior such as inputs, ephemeral responses, and autocomplete.
- **Contract / infrastructure** — persistence, protocol, configuration, migration, and integration contracts.

## local subscriber synchronization User Actor Discord auxiliary edges (Checkpoint 40)

- Accepted Follow owner notices use a durable at-most-once SQLite claim before one adapter-backed DM. Real follower lifecycle scenarios retain unchanged Accept/Undo/refollow semantics, while the new fake outer SDK transport verifies exact target ID, mention suppression, and loss of HTTP response without duplicate notification — `tests/behavior/test_user_actor_follow_scenarios.py`, `tests/test_user_actor_adapter_edges.py`.
- Durable partial Discord mutation reconciliation uses the actual `UserActorDeliveryService` and SQLite publication rows, a neutral adapter state snapshot, and rejects a wrong physical receipt or transient Discord GET without publishing an unintended Delete — `tests/test_user_actor_adapter_edges.py`, `tests/behavior/test_user_actor_content_fanout_scenarios.py`.

## local subscriber synchronization contract and Personal Feed timestamp regressions

- Source creation timestamps and optional edit author validation — `tests/test_platform_types.py`.
- SQLite UTC roundtrip, sequential ActivityPub Updates and invalid naive timestamp admission — `tests/behavior/test_personal_feed_conversations.py`.
- Static Compose gateway process secret allowlist — `fedify-gateway/src/test-docker-contract.ts`.

## local subscriber synchronization Personal Feed root create ingress (Checkpoint 21)

- A bound owner text-channel Create traverses adapter -> `PlatformEventIngress` -> `DiscordEventRouter` -> real SQLite publication, with deterministic AP identity and original timestamp. A user-created text/announcement discussion thread is then adopted through typed `ThreadCreated` against the published root; a wrong parent cannot rebind it. Wrong connector, unknown author, moved binding, absent source time, forum Create and unsupported attachments have no canonical publish side effect — `tests/test_personal_feed_created_ingress.py`.
- Existing personal feed publication duplicate-root and announcement-channel tests remain the behavioral regression references — `tests/behavior/test_personal_feed_root_publication.py`.

## local subscriber synchronization verified thread message transport (Checkpoint 23)

- Forum thread reply normalization preserves its exact container, verified parent, author, reply target and timestamp. Parent/guild mismatches fail before ingress; synthetic starter and unsupported attachments cause no canonical Create — `tests/test_thread_message_ingress.py`.
- Production forum and personal-feed reply Create cutover remains pending; these are transport-boundary tests, not a claim of completed end-to-end publication.

## local subscriber synchronization Personal Feed typed reply ingress (Checkpoint 24)

- Typed reply actions enter `PlatformEventIngress` → real `DiscordEventRouter` → real `PersonalFeedConversationRuntime` and SQLite, retaining registered User Actor identity, deterministic ActivityPub IDs, root and nested-parent ancestry. Duplicate notices persist once; `MESSAGE_CREATE` before `THREAD_CREATE` adopts only the exact published root. Parent/guild mismatch, foreign connector, opaque Discord ID, unregistered/bot author, missing permission, unsupported media and cross-root reply references do not produce canonical Create — `tests/test_personal_feed_reply_ingress.py`.
- Missing `Thread.parent` in the SDK cache is handled by an authoritative adapter lookup; unavailable parent fails before ingress, without changing the message into a root — `tests/test_thread_message_ingress.py`.
- Existing personal feed conversations direct-caller regression tests remain required until the forum SDK-shaped callback cutover is complete.
- The production `BridgeBot._on_message_admitted` gate admits an unadopted but published Personal Feed root before `THREAD_CREATE`; unrelated text threads are ignored and subscribed forum replies retain their legacy routing — `tests/test_bot_personal_reply_cutover.py` (SDK import-only stub where discord.py is unavailable).

## local subscriber synchronization typed forum starter publisher preparation (Checkpoint 25)

- A neutral `ThreadCreated` + `MessageView` uses the existing canonical post publication core with distinct starter/message IDs. Invalid connector, kind, guild, starter identity, source snowflakes, unknown content and bot author fail before the gateway; registration notice failures and ban-check uncertainty cannot authorize publication — `tests/test_typed_community_publish.py` (real SQLite, fake gateway/transport).
- The companion normalized comment publisher rejects bot/cross-connector/cross-thread/wrong-parent/invalid snowflake messages, reuses the unchanged canonical SQLite comment mapping, and delivers best-effort registration/ban notices without changing the refusal decision — `tests/test_typed_community_publish.py` (Checkpoint 26).
- Production forum callbacks still use the legacy path; only the SDK-free post/comment publisher is prepared. Full runtime cutover and sibling fanout require later verified bindings.

## local subscriber synchronization Local Community typed physical fan-out (Checkpoint 30)

- Real-SQLite host/local-subscriber physical thread copies, separate thread/starter IDs, idempotent replay, subscriber revocation, invalid-guild/destination receipts, per-target failure isolation, subscriber-to-host fan-out and nested per-destination parent preservation — `tests/test_neutral_local_community_fanout.py`.
- Previous SDK-backed local subscriber synchronization/4 Local Community behavior remains the regression reference; typed operations are preparatory and not yet wired into the live Gateway router.

## local subscriber synchronization Discord-originated Community mirror mutations (Checkpoint 38)

- Real SQLite source, mirror and inbound delivery ownership, exact normalized message edit/delete, preserved attribution, per-target failure isolation, raw-event echo tracking and source exclusion — `tests/test_community_source_mutations.py`.
- Revoked/rebound forum subscriptions, invalid guild metadata, mismatched fetched physical identity and adapter failure are rejected without a second SDK operation — `tests/test_community_source_mutations.py`.
- Legacy outbound and inbound mutation semantic regressions remain covered by `tests/test_phase8_edit_delete_sync.py` when the Discord SDK dependencies are available.

## local subscriber synchronization Gateway ingress transition

- Normalized raw edit/delete -> single domain-mutation sink, foreign-connector rejection, thread mutation no-op, unbound create rejection and real adapter admission — `tests/test_platform_event_ingress.py`.
- Typed mutation source/mirror ownership and partial-read reconciliation — `tests/test_typed_message_mutations.py`.
- Gateway shutdown admission and callback drain — `tests/test_discord_adapter_lifecycle.py`.
- Discord SDK lifetime vs both workers (normal exit, failure, TaskGroup cancellation), repeated cancellation of a callback drain — `tests/test_bot_shutdown_order.py`, `tests/test_discord_adapter_lifecycle.py`.
- Repeated cancellation while Personal Feed/User Actor recovery workers complete their own finalizers — `tests/test_worker_shutdown_cancellation.py`.
- Definitively invalid Personal Feed destination fails without another identical POST; temporary `unavailable` remains retryable — `tests/test_personal_feed_adapter_worker.py`.

## Registration and user identity

- Registration scenarios, duplicate handling, and user-facing registration outcomes — [`tests/behavior/test_registration_scenarios.py`](../../tests/behavior/test_registration_scenarios.py)
- Browser/OAuth registration flow and actor identity creation — [`tests/test_registration_flow.py`](../../tests/test_registration_flow.py)
- Registration command adapter — [`tests/commands/test_register_command.py`](../../tests/commands/test_register_command.py)
- Fediverse handle and actor URL rules — [`tests/test_fediverse_identity.py`](../../tests/test_fediverse_identity.py)
- User identity backup and restore tooling — [`tests/test_user_identity_dump.py`](../../tests/test_user_identity_dump.py)

## Community discovery and subscriptions

- Remote subscription lifecycle and accepted/pending behavior — [`tests/behavior/test_subscription_scenarios.py`](../../tests/behavior/test_subscription_scenarios.py)
- Follow/Accept subscription protocol flow — [`tests/test_follow_subscription_flow.py`](../../tests/test_follow_subscription_flow.py)
- cross-workflow register, subscribe, accept, publish, retry, and echo suppression — [`tests/behavior/test_end_to_end_bridge_scenarios.py`](../../tests/behavior/test_end_to_end_bridge_scenarios.py)
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

- local subscriber synchronization Remote Community typed inbound post, verified separate thread/starter IDs, replay, revoked subscription and foreign receipt rejection on real SQLite — `tests/test_normalized_inbound_remote_posts.py`.
- local subscriber synchronization preparatory typed inbound comments: mapped post delivery, per-recipient nested parent, foreign canonical parent rejection, deferred missing post/backfill, wrong receipt and withdrawn Follow — `tests/test_normalized_inbound_remote_comments.py`.
- local subscriber synchronization production Remote Community typed comment/backfill: comment-before-post, fail-closed incomplete parent deferral, distinct thread/starter IDs, repeated comment, wrong Page identity, revoked Follow, foreign physical receipt and remote fetch outage — `tests/test_normalized_ap_backfill.py`. Only network fetch/Discord HTTP are faked; SQLite and runtime are real.

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

- Local subscription control-plane behavior — [`tests/behavior/test_local_subscriber_scenarios.py`](../../tests/behavior/test_local_subscriber_scenarios.py)
- Host-originated fanout to local subscriber surfaces — [`tests/behavior/test_local_community_local_subscriber_sync_scenarios.py`](../../tests/behavior/test_local_community_local_subscriber_sync_scenarios.py)
- Subscriber-originated publishing, headers, reply mapping, and retries — [`tests/behavior/test_local_community_local_subscriber_origin_scenarios.py`](../../tests/behavior/test_local_community_local_subscriber_origin_scenarios.py)
- Participant edit/delete propagation — [`tests/behavior/test_local_community_participant_edit_delete_scenarios.py`](../../tests/behavior/test_local_community_participant_edit_delete_scenarios.py)
- Discord surface creation and placement — [`tests/behavior/test_local_community_surface_scenarios.py`](../../tests/behavior/test_local_community_surface_scenarios.py), [`tests/test_discord_forum_placement.py`](../../tests/test_discord_forum_placement.py)

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
- Schema cleanup and migration idempotency — [`tests/test_schema_cleanup.py`](../../tests/test_schema_cleanup.py)
- Removal of legacy remote-subscriber naming compatibility — [`tests/test_remote_subscriber_naming.py`](../../tests/test_remote_subscriber_naming.py)
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

## User profile/media initial persistence implementation

Behavior coverage lives in `test_user_profile_persistence_scenarios.py`, `test_profile_media_scenarios.py`, and `test_profile_media_backup_scenarios.py`. It covers fallback defaults, atomic metadata validation, SQLite invariants, canonical image publication, DB-backed HTTP delivery, and complete DB-plus-media backup/restore.

## Platform API physical surface mapping (inactive contract)

- Identity/hash invariants, conflicting inbound kind/guild detection, aware timestamp validation, raw update/delete representability, partial content, attempt and receipt scope, SDK-free event DTOs — [`tests/test_platform_types.py`](../../tests/test_platform_types.py).
- Two-connector registry and real SQLite physical-claim state; forum starter/replies, wrong-parent thread lookup/send rejection, root-anchored Personal Feed recovery, negative ACL/capabilities, and uncertain delivery without blind retry — [`tests/behavior/test_platform_api_contract.py`](../../tests/behavior/test_platform_api_contract.py).
- Existing production behavior remains covered by the Discord, local-community, Personal Feed, ActivityPub, and initial persistence implementation tests listed above and below; physical surface mapping does not activate a second event pipeline.

## Platform model initial persistence implementation

- Personal Feed service/repository agreement on physical claims and target-local parent resolution during upgrade — [`tests/test_platform_persistence_simplicity_regressions.py`](../../tests/test_platform_persistence_simplicity_regressions.py).

- Generated roles, owner exclusivity, scoped identity, parent-channel FKs, recipient selection, and retry CAS — [`tests/test_platform_data_model.py`](../../tests/test_platform_data_model.py).
- Claim kind admission for all owner types on both connectors, schema-only `create_all()` and explicit application bootstrap — [`tests/test_platform_persistence_review_regressions.py`](../../tests/test_platform_persistence_review_regressions.py).
- Released-source/fresh schema parity, current-only cascade/restart preservation, real replacement-window failures, concurrent migration runners, and FK enforcement after commit — [`tests/test_platform_data_model_migration.py`](../../tests/test_platform_data_model_migration.py).
- Mixed Discord/Fluxer post/comment routing, explicit/implicit Accept with global status promotion and Discord-only DMs, local publish fanout, HTTP dashboard, guild/guildless subscription lists, and Personal Feed view/mutation isolation — [`tests/behavior/test_connector_scoped_persistence.py`](../../tests/behavior/test_connector_scoped_persistence.py).
- `tests/fixtures/unsupported_prerelease_schema.sql` is a rejection-only unsupported development schema; supported upgrade fixtures come from released v0 SQL, independent tagged models and the tagged SQLite file.

- Released v0→current v1, source recognition, exact rollback, restored FK enforcement, semantic DDL parity, concurrent startup and migrated runtime actions — [`tests/test_legacy_v0_to_v1_migration.py`](../../tests/test_legacy_v0_to_v1_migration.py).

- Complete generated-role column admission: appended expression, wrapper, VIRTUAL mode rejection, formatting tolerance and unchanged database on rejection — [`tests/test_current_v1_generated_role_validation.py`](../../tests/test_current_v1_generated_role_validation.py).

- Real released AP namespaces, selective relationship reset, linked routing/JSON conversion, signed-payload/collision rejection, actor API, concurrent cutover and retained-claim retry — [`tests/test_legacy_identity_cutover.py`](../../tests/test_legacy_identity_cutover.py).

## Direct released SQLite upgrade (plan 147)

`tests/test_direct_sql_v0_to_v1_migration.py` covers native source staging before DROP, single ORM target creation, physical-window failures, TEMP cleanup, malformed integer-affinity snowflakes and pinned-source/schema provenance. `tests/test_legacy_v0_to_v1_migration.py` and `tests/test_legacy_identity_cutover.py` retain release, preservation, identity/owner, concurrency and runtime scenarios. Current-only descendants are checked directly on v1; no obsolete migration oracle remains. `tests/test_migration_cleanup.py` covers every released scalar relation missing an FK, ancestry cycles, pre-staging rejection and native index contract changes.

## local subscriber synchronization Local Community normalized runtime (pre-cutover)

- File-backed SQLite + real `LocalCommunityRuntime` + `ContentPublishService`:
  [`tests/test_normalized_local_community_runtime.py`](../../tests/test_normalized_local_community_runtime.py).
  Host/subscriber starter publication, physical source vs mirror ownership,
  sibling receipts, mapped root/nested comment parents, duplicate Gateway
  events, unknown/mismatched forum/connector/guild, removed subscriber and
  unregistered author, and a foreign known starter ID. HTTP and Discord
  SDK are the only faked boundaries.
- Physical receipt/idempotency regressions remain in
  [`tests/test_neutral_local_community_fanout.py`](../../tests/test_neutral_local_community_fanout.py).
  These tests do not claim that production callbacks have been switched.

## local subscriber synchronization Local Community Discord-originated source mutations (Checkpoint 39)

- `tests/test_local_source_mutations.py`: real SQLite host/subscriber starter and comment edit/delete, preserved attribution, source exclusion, authorized subscriber-origin edits, revoked subscriber source/destination, wrong physical fetch receipt, isolated transport failure, and forbidden SDK fallback.
- Neighboring `test_local_inbound_mutations.py`, `test_normalized_local_community_runtime.py`, `test_neutral_local_community_fanout.py`, Remote Community and AP inbound mutation tests jointly verify canonical routing and shared surface boundary.
- Full installed-Discord SDK behavior still requires an integration run. A test-only import stub supplies no Discord transport behavior.

## Stage 04 multi-forum adversarial scenarios (Checkpoint 63, partial)

- Real SQLite Local Community author in subscriber forum B, independent
  host A and subscriber C forum receipts, no echo/duplicate POST and
  one-target uncertainty isolation. The same canonical nested comment uses
  **different** confirmed parent copies in A and C; a parent's ID from A
  cannot authorize a copy in C —
  `tests/behavior/test_platform_message_fanout_scenarios.py`.
- Interrupted pending dispatch, unknown after accepted POST, exact
  thread/starter receipt scope and retained orphan after surface rebinding;
  nested comment stays blocked until the exact physical parent arrives,
  then materializes its immutable authored body and target-local reply ref —
  `tests/behavior/test_platform_delivery_recovery_scenarios.py`. The existing
  `PlatformFanoutCoordinator` delegates its startup scan to the neutral
  `delivery_recovery.py` helper, not to an additional network worker.
- Static shared-boundary checks reject Discord integer/source projections
  in coordinator and intent repository, while community comment rendering
  preserves the existing attribution and bounded message presentation —
  `tests/behavior/test_platform_message_fanout_scenarios.py`.
- Deterministic 35-trace state-machine checks over real SQLite for
  deleted-before-send, delivered, unknown, rebound target and mismatched
  physical mode; terminal attempts cannot be claimed a second time —
  `tests/model_based/test_platform_message_fanout_state_machine.py`.

These tests use local transport fakes and are not a replacement for the
full Python/Node suites or live Discord/Fedify integration acceptance.


## Stage 04 inherited v0 mutations

- `tests/behavior/test_stage04_tagged_legacy_mutations.py`: authentic release-code
  database creation and startup migration followed by real Local/Remote first
  Update/Delete handlers; original-author evidence, rejected forgery, missing or
  conflicting provenance, human host/subscriber sources, nested replies, durable
  mirror ACKs, unknown write outcomes and restart.
- `tests/test_stage04_physical_mutation_recovery.py`: positive physical author
  lookup, cancellation before writes, exact mutation claims and replay barriers.
- `tests/test_stage04_inbound_canonical_snapshots.py` and
  `tests/test_stage04_delivery_intents.py`: all five formerly hidden scenarios
  now belong to collected test classes; canonical tombstones and unsent/unknown
  Create cancellation remain distinct.


## Rendered User Actor mutation commit recovery

- `tests/behavior/test_canonical_ready_commit_recovery.py`: failed local commit
  after render, durable error/backoff, immutable canonical JSON across restart,
  a confirmed typed source with injected SQL failure, active lease protection,
  stale-owner CAS and a revision conflict without overwriting newer content.
