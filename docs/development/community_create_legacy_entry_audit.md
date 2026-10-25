# Audit: duplicate raw Discord Community Create pipeline

**Performed before code changes.** Source: the user-provided durable Community fanout patch archive, applied in manifest order to clean Git base `53e9311958959c925c7522fe629d66690ae9ce1f`. Patch pack declares expected resulting tree `d8bf4f457b9f81328e4f67530776be46090870f3`; source is an **uncommitted** worktree. Audit reflects static call graph and explicit repository tests, not unknown runtime monkeypatches or external consumers.

## 1. Confirmed production call graph

- `src/discord_bot.py::on_thread_create/on_message` call `_require_discord_adapter().ingest_thread_created/ingest_message_created` (rather than router raw functions) for Discord community sources; `src/platform_event_ingress.py::handle_platform_event` dispatches typed `ThreadCreated/MessageCreated` to `src/discord_event_router.py::handle_platform_thread_create/handle_platform_message_create`. Those call `CommunityRuntime.handle_normalized_*` or `LocalCommunityRuntime.handle_normalized_*`; the latter use `ContentPublishService` render-only canonical admission and `PlatformFanoutCoordinator` for physical copies.
- **Parallel raw Create entry** `DiscordEventRouter.handle_thread_create/handle_message` routes SDK objects to `CommunityRuntime.handle_discord_thread_create/handle_discord_message` and `LocalCommunityRuntime.handle_discord_thread_create/handle_discord_message`. Remote raw runtime independently performs AP publication, source-group insertion, mirror fan-out and delivery persistence; local raw runtime delegates to `LocalCommunityDiscordCreateHandler.handle_host_*` and `handle_local_subscriber_*`, also performing duplicate canonical/persistence/fanout logic. `src/community_sync/discord_fanout.py` and `src/local_communities/discord_fanout.py` still contain SDK `thread.send`/`forum.create_thread` methods supporting raw callbacks, though some typed consumers of these collaborators remain: **do not delete entire modules without precise method callers**.
- No explicit `src/` callers found for router `handle_thread_create()` or `handle_message()`. This **does not prove** no dynamic consumers; Python public APIs could be called by external plugins/scripts. No identified production configuration binds those raw router methods after the typed Community Create cutover.
- `DiscordEventRouter.handle_message_edit/delete` and corresponding runtime mutation APIs are **outside this Create-entry audit**. `handle_personal_feed_thread_create`, normalized Personal Feed, `DiscordAdapter`, typed ingress, physical mutation recovery and incoming ActivityPub deliveries must remain unaffected.

## 2. Verified test and compatibility consumers

A search of `tests/**/*.py` finds **62 syntactic direct method invocations across 14 test files** (not just the three suites referenced in the review). In addition, several suites create mock attributes and assert calls (not counted as invocations). Consumer files are:

- `tests/behavior/test_local_community_publish_scenarios.py` (10)
- `tests/behavior/test_local_community_local_subscriber_origin_scenarios.py` (9)
- `tests/behavior/test_local_community_local_subscriber_sync_scenarios.py` (9)
- `tests/behavior/test_local_community_surface_scenarios.py` (3)
- `tests/behavior/test_comment_lifecycle_handlers.py` (3)
- `tests/behavior/test_local_community_disabled_scenarios.py` (1)
- `tests/behavior/test_connector_scoped_persistence.py` (1)
- `tests/test_community_runtime_scenarios.py` (2)
- `tests/test_phase2_fanout_scenarios.py` (4)
- `tests/test_phase3_message_fanout_scenarios.py` (5)
- `tests/test_phase4_reply_preservation.py` (5)
- `tests/test_phase5_inbound_ap_shared_groups.py` (1)
- `tests/test_phase9_bidirectional_mirror_messages.py` (8)
- `tests/test_legacy_v0_to_v1_migration.py` (1)

`tests/test_production_forum_callbacks.py`, `tests/behavior/test_personal_feed_runtime_paths.py` and `tests/test_bot_personal_reply_cutover.py` also use raw method names **as mock methods**, not tested bridge calls. The existing normalized coverage in `tests/test_normalized_community_runtime.py`, `tests/test_normalized_local_community_runtime.py`, `tests/test_scoped_community_publication.py`, `tests/test_remote_community_typed_receipts.py`, `tests/behavior/test_local_community_receipt_handoff.py` and others already checks most typed source identity, authorization, deduplication, fanout, nested parent and failure invariants; exact case-by-case migration mapping must be established before deleting old coverage.

## 3. Risks, deletion boundary, and decision

**Decision:** removing duplicate Create entry points simplifies production: remove the uncalled raw `DiscordEventRouter` create entry and SDK-shaped `CommunityRuntime`/`LocalCommunityRuntime` create entry points **after identifying/migrating tests**, without modifying normalized ingress or Personal Feed. Do not preserve a raw SDK-to-typed fallback in production. Remove now-unreachable `LocalCommunityDiscordCreateHandler` raw host/subscriber helpers and SDK-only fanout methods *only if* their complete method-level caller analysis proves absence of typed/AP/recovery consumers.

**Blocking compatibility detail:** review's “three legacy suites” inventory was incomplete. Removing runtime raw entry points will invalidate more than a dozen independent test modules unless their tests are converted to normalized runtime handlers or obsolete tests are replaced with equivalent observable scenarios. Silently deleting the tests or adding `__getattr__` / alias adapters would not meet preservation or single-path requirements. External callers cannot be ruled out statically; raw methods are not a documented supported platform API, but removal should be called out as intentional breaking change with no fallback.

**Safety invariants to preserve:** exact connector+container+message source identity, author and guild policy, current subscription/host ownership, canonical Create admission before physical send, one AP Create on retries, per-target durable receipt and orphan/unknown behavior, parent mapping (wait rather than flatten for known missing parents), physical source vs mirror dedup; Personal Feed still works; inbound AP and Update/Delete remain on their separate owners.

**TDD plan:** (1) record current targeted test status; (2) add normalized runtime scenarios and migrate direct raw tests before removing each last raw owner; (3) remove dead raw router/runtime/create methods, narrow imports, update event-flow docs and known-issues journal; (4) validate absence by AST/grep, SQLite scenario suites, and Python compile checks; preserve baseline failures separately. Do **not** present removal as complete if the 14 test files still rely on deleted APIs.

## 4. Scope limitations

No live bot/Fedify run or installed external Python/Node dependencies is implied by this static audit. Runtime reflection/dynamic consumers outside this repository cannot be proven absent by grep. Keep explicit evidence of methods that remain for inbound AP or physical mutation paths and do not conflate them with Discord-authored raw Create.

## 5. Final consumer migration

The final 32 direct runtime calls in eight remaining files now enter typed
handlers through source-observation helpers in `tests/support/community_events.py`.
The four runtime raw Create methods and four local raw host/subscriber handlers
have been deleted. Method-level inspection also removed the SDK-only Local and
Remote fanout Create helpers; their last direct edge tests now exercise typed
fanout with real canonical rows. SDK physical Update/Delete remain intact.

The original inventory above is historical evidence. Static AST checks now
assert absence of raw runtime APIs as well as router dispatch. No in-repository
dynamic binding to the removed methods was found. External callers remain an
intentional compatibility break requiring verified platform ingress.
