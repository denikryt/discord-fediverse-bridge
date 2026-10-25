# Event flows

This document explains the major runtime traces through the bridge: user/platform action, important preconditions, modules called, database effects, and outbound effects. It owns step-by-step behavior flow descriptions, not exhaustive function internals.

## Stage 04 typed forum source admission (checkpoint 57 foundation)

A verified forum `ChannelRef` enters `content_sync.surface_resolution` with
the actual connector, parent forum kind, and guild identity. The resolver
queries `ChannelSurface` and the owner row together: active Local host, active
Local subscriber with active owning community, or accepted Remote subscription.
Unknown metadata and an inactive/rebound claim cannot publish. The frozen
`ResolvedForumSource` is a candidate: persistence and later sends must
revalidate it in the SQLite writer transaction. Exact authored source lookups
use `(connector, immediate container, message id)` rather than global Discord
message IDs. Remaining old Discord event routes are replaced in checkpoint 58,
not shadowed by a parallel live ingress.

## Stage 04 community Create — render, atomic admission, signed send

1. A verified Discord forum starter or reply enters the single normalized
   `PlatformEventIngress`; the runtime selects the exact current Local or
   Remote Community owner by connector, forum surface and policy. Bot, webhook,
   unknown and mismatched starter/reply sources cannot claim canonical authorship.
2. `ContentPublishService` derives stable Page/Note/Create IDs from the exact
   `(connector, immediate container, physical message)` source and uses its
   original timezone-aware `created_at` rather than Gateway receipt time.
   Fedify `/render-community` validates these IDs and returns JSON, **without**
   signed network delivery. Legacy `/publish` routes now reject send-before-save.
3. One SQLite writer transaction saves the source mapping, authored AP object,
   Local/Remote canonical post or comment, source physical receipt and
   `UserActorOutboundSource`. The same commit adds authorized Remote Group
   direct recipients and/or immutable Local Group Announce snapshots. If source
   ownership, parent or any identity changes, the whole graph rolls back.
4. After commit, the existing User Actor worker may send a stored signed User
   activity to a verified direct community actor; Local Group announcements
   use the existing federation worker and Group signing identity. A current
   accepted Follow incarnation and inbox remain required at both claim and
   pre-POST checks. No followers is a valid committed canonical publication.
5. Duplicate Gateway ingress adopts the original canonical graph; a failed
   recipient does not authorize a fresh ActivityPub Create ID. Later physical
   fanout, edits and crash recovery belong to subsequent Stage 04 checkpoints.

## Registration flow

1. `/register` starts in `src/commands/register.py` and browser routes in `src/http_api.py`.
2. Discord OAuth state is stored through `src/registration_service.py`.
3. Successful completion creates a `users` row with stable local actor URLs and key material.

## User Actor Follow notice and Discord mutation reconciliation (local subscriber synchronization Checkpoint 40)

1. An accepted ActivityPub Follow to a registered User Actor is persisted through `UserActorFollowService`; the signed Accept is sent through Fedify. The follower repository claims the owner notification **before** attempting Discord I/O, preserving at-most-once delivery even if the HTTP response is lost.
2. The service calls `DiscordAdapter.send_owner_notice(discord_user_id, content)`, not `BridgeBot.fetch_user()` or a generic `PlatformDeliveryPort` DM operation. The adapter verifies the target user ID and disables mention parsing. DM failures are logged; they do not undo the accepted Follow or re-enable a notification retry.
3. For a durable partial Discord edit, `UserActorDeliveryService.drain_reconciliations()` calls `DiscordAdapter.fetch_message_state(channel_id, message_id)`. The adapter verifies the actual physical message/container IDs and returns an SDK-free content/timestamp snapshot. A verified 404 represents deletion; malformed receipts, attachments and transient reads remain reconciliation failures, never invented ActivityPub Deletes.
4. Both operations join the adapter's SDK I/O drain before its borrowed Discord client is closed. This does not change Service→Community Follow ownership or add generic platform DMs.

## Remote /subscribe-community flow

1. `src/commands/subscribe.py` calls `src/operations/subscribe.py`.
2. `src/lemmy_client.py` resolves the remote community when needed.
3. `src/db/database.py` records `channel_community_subscriptions` and bridge follow state.
4. `src/fedify_gateway_client.py` calls gateway `/follow-community`; gateway sends the Follow.
5. Accept returns through gateway `/inbox`, `src/http_api.py`, and `src/activitypub_handlers.py`.

Fedify authenticates and queues the inbound `Accept(Follow)`. The gateway
extracts a Follow ID only from that accepted activity; a missing ID or
unrelated embedded activity produces no Python event. Python then matches the
ID and Accept actor to the stored pending bridge Follow before marking channel
subscriptions accepted.

## Remote /unsubscribe-channel flow

1. `src/commands/unsubscribe.py` calls `src/operations/unsubscribe.py`.
2. The channel subscription is removed.
3. If it was the last channel for the community, Python calls gateway `/unfollow-community`.
4. The bridge follow row is removed or preserved according to Undo(Follow) delivery outcome.

## Discord Local Community source edit/delete (local subscriber synchronization Checkpoint 39)

1. Typed Discord Gateway mutation ingress retains the original physical message ID; `DiscordEventRouter` resolves a persisted Local Community starter or comment surface before invoking `LocalCommunityRuntime`.
2. `LocalCommunityMutationHandler` verifies source community lifecycle and active subscriber-origin authority (if applicable), finds the persisted canonical AP object, and submits its Update/Delete through the existing federation service.
3. The runtime-bound `DiscordAdapter` is passed to `LocalCommunitySurfaceFanout`, which enumerates canonical host and subscriber surface receipts. It omits the physical source copy.
4. Before each target edit/tombstone, `LocalCommunityDiscordFanout` rechecks current host placement or active local subscriber identity, including original subscriber row ID, community, guild policy and parent thread mapping. Historical receipts alone never authorize a mutation.
5. Edits fetch an exact scoped `MessageRef`, preserve each copy's existing attribution header, and refuse a mismatched/unknown read. Deletes keep the visible `*deleted by creator*` marker. Per-target failure does not issue a legacy SDK fallback or block healthy copies.
6. These are best-effort mutations; there is no new delivery queue or local subscriber participation recovery. Direct SDK test callers without a bound adapter remain supported until the final dependency-audit cutover.

## Discord Personal Feed root -> ActivityPub Create (local subscriber synchronization)

1. A bound text/announcement-channel `on_message` passes the synchronous Gateway admission gate and checks the current root binding. Bot-authored messages remain excluded.
2. `DiscordAdapter.ingest_message_created()` confirms the physical text/announcement kind, refuses unsupported attachments, and converts the SDK message to `MessageCreated` with original `created_at` and connector-scoped IDs.
3. `PlatformEventIngress` rejects another connector before `DiscordEventRouter.handle_platform_message_create()` verifies the root placement, source guild, human author and current binding. Forum/thread creates are not acknowledged by this partial route.
4. `PersonalFeedPublisher.publish_normalized_root()` rechecks the current owner binding, registered User Actor, ban/guild policy and source timestamp. It preserves the prior deterministic ActivityPub identity and persists once; duplicate Gateway replay does not render again.
5. A later `on_thread_create` for the published text/announcement root enters `DiscordAdapter.ingest_thread_created()` and the same typed sink. `DiscordEventRouter.handle_platform_thread_create()` adopts the exact root/thread ID only when the original publication and parent channel agree. No guessed forum starter or history fallback is needed.
6. A `MESSAGE_CREATE` inside an adopted Personal Feed discussion thread now enters `DiscordAdapter.ingest_thread_message_created()` and the same typed sink. Its parent/guild/author/source timestamp/reply reference are verified before `DiscordEventRouter.handle_platform_message_create()` routes to `PersonalFeedConversationRuntime.publish_normalized_reply()`.
7. The conversation runtime rechecks the persisted root parent, current channel owner, guild policy, registered author and bans. `MESSAGE_CREATE` arriving before `THREAD_CREATE` can adopt the exact already published root; duplicate replies reuse deterministic ActivityPub IDs and the existing SQLite mapping. A nested reference is accepted only if its canonical object belongs to the same root.
8. An unregistered reply is rejected before federation. A best-effort registration notice uses `DiscordAdapter.send_message()` with a deterministic `CreateAttempt`; failed notice delivery does not change the rejection. Bot/unknown authors and unsupported media cannot publish.
9. Other community forum-thread and reply Create callbacks still use their SDK-shaped domain path until their own typed cutover. No duplicate publication route is active for bound Personal Feed roots or replies.

## Discord thread message observation (local subscriber synchronization preparatory boundary)

1. `DiscordAdapter.ingest_thread_message_created()` verifies a physical `discord.Thread`, its independently observed parent, and matching message/thread/parent guild IDs; ambiguous metadata fails before domain admission.
2. The adapter refuses unsupported attachments instead of discarding them and emits one `MessageCreated` with its immediate thread container, parent channel kind, reply reference, actual author and original timestamp. Discord's synthetic `THREAD_STARTER_MESSAGE` emits nothing.
3. The production Personal Feed reply callback now uses this neutral event; subscribed/local community forum replies still call their existing SDK-shaped runtime. The physical parent may be fetched via the adapter when absent from the SDK cache; a failed/conflicting lookup does not guess the parent or convert the message into a root.

## Remote community forum publication through typed runtime (local subscriber synchronization pre-cutover)

1. A verified forum `ThreadCreated` plus separately verified starter `MessageView` enters `CommunityRuntime.handle_normalized_thread_create()` (preparatory route, not yet bound to production callbacks). The runtime checks the current accepted source subscription, persisted guild and federation policy before any ActivityPub publish.
2. The canonical `ContentPublishService.publish_normalized_post_to_community()` verifies the author and publishes using the existing gateway and persistence. The runtime records the actual thread ID and distinct starter ID in `CommunityThreadGroup` and the source delivery row; each current accepted sibling is sent via `DiscordFanout.mirror_normalized_thread_to_siblings()` and `PlatformDeliveryPort`.
3. A verified `MessageCreated` inside any mapped subscribed forum enters `handle_normalized_message_create()`. Existing physical deliveries and original starters cannot be republished; new authored messages in either a source or mirror thread can become canonical comments.
4. `resolve_normalized_reply_context()` validates source physical thread membership and maps a source starter or persisted parent comment to the correct target-specific parent references. Unknown referenced messages retain the legacy flat-send policy; cross-thread or cross-connector references fail before federation.
5. The shared content publisher creates one ActivityPub comment; message-group and per-target SQLite receipts preserve source/mirror mapping. Each target failure is isolated. **These functions are not wired to the Gateway yet:** local-community routing and forum production callbacks still require their coordinated cutover, to avoid two active publication paths.

## Discord thread -> remote Lemmy community publish flow

1. `src/discord_bot.py` receives a Discord event.
2. `src/discord_event_router.py` selects remote subscription runtime.
3. `src/community_sync/runtime.py` validates context.
4. `src/content_sync/outbound_publish.py` builds the publish request.
5. Gateway `/publish` signs and sends Create; Python persists object/mapping state.

## Inbound receipt observability

`src/http_api.py` creates an `in_progress` receipt before dispatch. Terminal handlers return a lifecycle `status`, a stable `InboundActivityOutcome`, and human-readable `detail`; `EventReceiptRepository` persists all three together. Deferred or failed retries reset the row to `in_progress` and clear the previous outcome. Duplicate terminal deliveries return the stored outcome without rewriting the receipt.

## Remote ActivityPub post -> Discord fanout flow

1. Gateway `/inbox` receives ActivityPub. A wrapped `Announce(Create)` uses the accepted delivery payload and checks its entire nested activity against Fedify's typed outer activity before origin-author verification and normalization to `post.created`; a direct Create must match its Fedify-authenticated actor and embedded object author.
2. `src/activitypub_handlers.py` validates idempotency and routes to remote subscription runtime.
3. If no accepted subscription and no existing mapped thread context exists, Python skips the event locally while gateway delivery remains acknowledged.
4. In production `CommunityRuntime.handle_normalized_inbound_post()` obtains current accepted subscription targets and checked guild metadata before creating Discord forum threads through `PlatformDeliveryPort.create_forum_thread()`. Each destination gets one stable `CreateAttempt`; the adapter owns the physical Discord permission check and POST. The canonical `CommunityThreadGroup` and per-destination delivery rows record the verified **separate** thread and starter IDs. A repeated mapped event never submits another create.
5. Each accepted forum is attempted independently: a rejected or unverified Create receipt cannot block later healthy recipients and never records a physical mapping. If every initial attempt fails, the original error is surfaced; successful physical copies remain durable. Normal duplicate Create and parent backfill never resend into gaps of an already persisted group: a missing receipt is not evidence of a negative Discord create. Recovery of existing partial groups requires local subscriber participation verified physical reconciliation.
6. A missing guild, revoked subscription, or mismatched transport receipt cannot authorize a persisted physical copy. The legacy SDK path remains only for isolated compatibility tests while the remaining inbound comment/backfill paths await local subscriber synchronization migration.

## Remote ActivityPub comment -> Discord fanout flow

1. Gateway validates direct/forwarded ActivityPub author provenance before normalizing a comment to `comment.created`; failed origin proof has no Python-side effects.
2. Python validates the event and checks accepted subscription or already mapped parent/post context.
3. Unsubscribed comments without mapped context are skipped before backfill or Discord fanout.
4. Production with a registered `DiscordAdapter` calls `CommunityRuntime.handle_normalized_inbound_comment_with_backfill()` through the same ActivityPub dispatcher. When the post group is entirely absent, `backfill_normalized_post_as_thread_group()` retrieves the exact Page/Article post identity and calls the typed post writer. When a group exists but any accepted forum lacks a confirmed physical receipt, it defers dependent comments without fetching the post or issuing another Discord Create; the missing row could follow a lost HTTP response. Revoked subscriptions cannot authorize new delivery and every physical receipt is checked before persistence.
5. If the remote document cannot be fetched, has a different object ID or type, or any accepted destination still lacks a confirmed thread mapping, the comment remains `deferred` and no child Discord POST occurs. Normal `post.created` duplicates still skip rather than triggering recovery. A second comment delivery of an already-mapped object skips without refetching the parent.
6. Per-thread message Create failures are isolated: only verified physical receipts create delivery rows. An all-target failure is surfaced, and ordinary duplicate Create never blindly replays a possible delivered message. Recovery across process restarts without proof remains local subscriber participation.
7. Once the parent post is fully mapped, `handle_normalized_inbound_comment()` uses `PlatformDeliveryPort.send_message()` with per-destination physical parent references. Existing canonical message-group and SQLite delivery mappings remain authoritative. Known cross-post parent references are rejected; unknown comment parent mapping keeps the legacy flat-send policy. The legacy SDK path is reachable only through direct compatibility callers without a registered adapter, not parallel to production typed delivery.
7. **Checkpoint 41 security boundary:** Python `_fetch_ap_object()` now delegates to `activitypub_safe_fetch.fetch_public_ap_object()`: HTTPS-only, socket-checked public DNS, no implicit proxies/redirects, TLS hostname validation and bounded JSON. Tests cover unsafe URL, mixed/rebinding DNS, socket checks, redirect and oversized bodies. The local security tests do not establish a full dial-level integration with the installed runtime; production-ready local subscriber synchronization still requires installed-SDK/Fedify integration, native gateway SSRF tests and the locked Fedify security upgrade.


## Inbound local-community replay admission (local subscriber synchronization)

When an incoming Create matches a previously stored local-community post or
comment, `src/local_communities/inbound_create.py` checks current Follow state,
canonical identity and retained immutable source **before** running a new
Discord sibling fanout or ActivityPub relay. A changed activity, parent, author
or source rejects the replay. Historical rows without stored origin proof skip
recovery; a verified comment may recreate its missing generic AP mapping only
from an existing host receipt. If a previously authenticated generic mapping
already identifies the same AP activity, object and author, an additional
Local Community Discord copy retains that generic mapping's original physical
receipt; a different physical message is recorded only in Local Community
surface state. Conflicting activity/author identity still fails closed. The
Fedify gateway must independently validate
the claimed ActivityPub author before these checks count as authentication.

## Discord edit/delete -> ActivityPub update/delete flow

1. `BridgeBot.on_raw_message_edit` / `on_raw_message_delete` enters the tracked Gateway callback lifetime, and `DiscordAdapter` normalizes the SDK payload once into `MessageUpdated` / `MessageDeleted`.
2. `src/platform_event_ingress.py` checks connector scope before delegating to the **typed** methods in `src/discord_message_mutations.py`. The mutation handler retains echo suppression, partial-read reconciliation through `DiscordAdapter.fetch_message()`, and the existing `DiscordMutationContext`. The old raw handler is not invoked by production callbacks.
3. The selected runtime resolves persisted ActivityPub object ids through `src/db/database.py` and uses the context's narrow delivery wake/gateway capabilities for outbound Update/Delete. For Remote Community source mutations, `CommunityRuntime.handle_discord_message_edit/delete()` selects persisted mirror/non-source delivery rows and `DiscordFanout.propagate_edit/delete()` uses the registered `DiscordAdapter` for physical edits/deletes. Before each write, the fanout rechecks the current accepted subscription, community actor and allowed guild; historical receipts alone never authorize a mutation. An edit fetches and verifies the exact `MessageRef` to preserve the existing author header, while delete removes only the selected physical message. A failing target cannot cancel the others or trigger an SDK fallback; echo tracking records only successful physical mutations. The SDK path remains for direct compatibility callers without the adapter until the remainder of local subscriber synchronization is removed.
4. `src/content_sync/edit_delete.py` prepares the request.
5. Gateway `/update` or `/delete` signs and sends the ActivityPub operation.

## Discord raw thread mutation notices (local subscriber synchronization)

`BridgeBot.on_raw_thread_update` / `on_raw_thread_delete` enters the same tracked Gateway callback lifetime. `DiscordAdapter` preserves raw identity through `ThreadUpdated` / `ThreadDeleted`; `PlatformEventIngress` logs an explicit no-op. A physical thread rename or removal is **not** automatically an ActivityPub Update/Delete because no current canonical thread-mutation handler owns it. Source thread/message creation is still on the legacy SDK path pending the atomic typed Create cutover.

## Discord Gateway shutdown ordering (local subscriber synchronization)

`src/app.py::_run_bot_lifetime()` keeps the SDK context open while it closes Gateway callback admission, joins independently scheduled ingress callbacks, stops Personal Feed Discord delivery and User Actor delivery workers, and drains physical Discord adapter I/O. Only then does `BridgeBot.close()` close the Discord SDK. `Runtime.close()` owns Fedify/OAuth/database shutdown afterward; its idempotent worker-stop paths cover partially started processes. Cancellation during ingress drain or either worker stop is propagated **after** the underlying async task finalizers have completed, not by abandoning work against resources about to be closed.

## /create_community flow

1. `src/commands/create_community.py` calls `src/operations/create_community.py`.
2. `src/local_communities/service.py` creates stable local community actor metadata.
3. Python stores `local_communities`; gateway later serves the actor from DB.

## Discord thread -> local ActivityPub community publish flow

1. Router selects `src/local_communities/runtime.py` for the host forum or an active local subscriber forum of a local community.
2. Python records one canonical local-community thread/message row and a source Discord surface. Host sources create a `role="host"` source surface; local subscriber sources create a `role="local_subscriber"` source surface.
3. Python calls gateway `/publish-local-community`; gateway creates local ActivityPub content and delivers it to accepted remote subscribers.
4. Local Discord fanout creates missing target surfaces: host sources copy to local subscribers, while local subscriber sources copy to the host forum and sibling local subscribers.

## Remote ActivityPub Follow -> local community remote-subscriber flow

1. Gateway verifies the signed Follow to a local community actor, queues it, and dereferences the remote actor through Fedify's validated document loader. Only a matching actor document with admitted delivery endpoints emits `local.follow_requested`; HTTP 202 alone confirms queue admission, not this event.
2. Python stores `remote_subscribers` and asks gateway to send Accept(Follow).

## Accept local community follow flow

1. Local runtime accepts a remote follow.
2. Python calls gateway `/accept-local-community-follow`.
3. Gateway signs Accept(Follow) as the local community actor.

## Remote ActivityPub content -> local community Discord/federation relay flow

1. Gateway normalizes content addressed to a local community.
2. Python routes by `community_actor_id` to local community runtime.
3. Runtime mirrors into the host Discord forum and records canonical rows plus host surfaces.
4. Local Discord fanout creates missing local subscriber surfaces for active same-instance subscriber forums.
5. Gateway `/send-local-community-relay` signs and delivers rendered relay activities to other accepted remote subscribers.

## Local community edit/delete flow

1. Discord edit/delete enters through `src/discord_message_mutations.py`, then `src/discord_event_router.py` resolves the starter/reply surface row for the raw message id.
2. `src/local_communities/runtime.py` resolves the canonical post/comment and loads the `PublishedActivityObject` by canonical AP object id, not by the copied Discord message id.
3. For host or active local-subscriber source surfaces, Python sends one gateway Update/Delete and `src/local_communities/discord_fanout.py` mutates every other persisted local Discord surface.
4. For inbound remote update/delete, the runtime mutates every persisted local Discord surface, including host and local subscribers, then keeps the existing remote relay eligibility rule for remote subscribers.
5. Inactive local-subscriber surfaces are historical copies, not authoritative mutation sources.

## Remote ActivityPub Undo(Follow) -> local community unfollow flow

1. Gateway receives a signed embedded Undo(Follow), then Fedify queues its authenticated payload. The listener uses that accepted payload and checks the embedded Follow ID, actor, and target against Fedify's typed Undo; a later same-ID request cannot replace it through the raw cache.
2. Gateway emits `local.unfollow_requested` when the verified target actor is owned by this bridge.
3. Python removes the remote actor from `remote_subscribers`.
4. Future create, update, and delete fanout uses current accepted remote subscribers and excludes the removed actor.


## Local subscriber Discord post/comment -> local community flow

1. `src/discord_event_router.py` resolves the source forum through `src/local_communities/participant_routing.py`.
2. Active `LocalSubscriber` forums route into `LocalCommunityRuntime`; inactive subscribers and unrelated forums do not.
3. Runtime publishes the source Discord starter/reply through the existing local-community publish path and persists one canonical row.
4. Runtime records the source `role="local_subscriber"` surface and then fans out to the host forum plus sibling active local subscribers.
5. participant mutation support handles later edits/deletes from the source local-subscriber surface through the participant-wide mutation flow above.


## Disabled local community lifecycle flow

1. `/edit-community` opens a Discord modal for owner/super-admin callers and can save `status=active` or `status=disabled` alongside display metadata.
2. `src/local_community_lifecycle.py` is the shared lifecycle decision point; disabled communities keep existing DB state but reject new side effects.
3. Inbound ActivityPub deliveries targeting a disabled local community are acknowledged and routed through the existing skipped result path before domain mutation, fanout, subscriber creation, or outbound federation.
4. Discord-originated post, comment, edit, delete, and local subscription paths resolve the local community and stop before publish, persistence, relay, or Discord fanout when status is disabled.
5. Existing actor/object routes, content rows, subscribers, and Discord surfaces remain readable and unchanged; re-enabling returns existing subscriber rows to normal fanout behavior.

## Personal-feed binding and personal feed control plane routing flow

1. `/account-settings feed-bind` resolves the invoking registered user, checks the existing guild policy, requires Discord Manage Server permission, and accepts protocol `GUILD_TEXT` and `GUILD_ANNOUNCEMENT` channels.
2. The command snapshots the caller's current binding and submits one cancellation-safe synchronous mutation to `PersonalFeedService`.
3. The service acquires SQLite's writer reservation, rechecks user/guild/channel uniqueness and cross-mode channel ownership, then inserts or compare-and-set moves the single binding.
4. `/account-settings feed-view` reads the durable assignment; `feed-unbind` removes only the caller's current assignment. Historical publication/thread/message mappings are not modified.
5. `BridgeBot.on_message` recognizes owner-authored root messages in an active ordinary text-channel or announcement-channel binding before existing forum routing. Non-owner roots, stale bindings, threads, DMs, and unrelated channels do not classify as personal-feed roots.

## local subscriber synchronization normalized Discord Gateway event boundary (in progress)

`src/discord_gateway_events.py` converts observed SDK callbacks into the frozen
`ThreadCreated`/`ThreadUpdated`/`ThreadDeleted` and `MessageCreated`/`MessageUpdated`/
`MessageDeleted` platform types. It keeps the immediate message container and
source timestamp. An absent raw edit body or missing starter evidence stays
unknown; raw delete never needs cache access. A message with a known
thread `parent_id` but an uncached parent is rejected for later verified
resolution, never reclassified as a root text-channel message. Proven bot/webhook identities and
guild/channel metadata are checked before any domain publication. The live
`BridgeBot` to `PlatformEventIngress` cutover remains pending, so these DTO
conversions are not yet the deployed callback path.

The mutation boundary also accepts typed `MessageUpdated`/`MessageDeleted` events.
Before reading Discord-scoped database keys it validates the connector and
physical snowflakes; source/mirror echo policy is unchanged. Partial edits
resolve an authoritative `MessageView` via `PlatformAdapterPort.fetch_message`,
and transient read failure schedules the existing durable reconciliation. This
is a preparatory path, not yet wired into live Gateway callbacks.

## local subscriber synchronization Personal Feed inbound Discord delivery safety

1. An authorized remote Create, Update or Delete mutates the canonical inbound conversation object under `BEGIN IMMEDIATE`; each due claim captures the exact revision fingerprint and deleted flag before physical Discord work.
2. The Personal Feed worker rechecks the active binding. Before the first unknown-ID message send, it durably marks `CREATE_STARTED_DISCORD_SENTINEL`; the network call runs outside the SQLite transaction.
3. An interrupted or ambiguous first Create is parked with `UNCERTAIN_DISCORD_CREATE_ERROR`, not retried blindly. Later ActivityPub Update/Delete changes the canonical object but cannot clear this marker or trigger another Create.
4. On a verified Discord receipt, `record_discord_attempt` saves the physical ID and compares the completed claim to the current canonical revision. A superseded edit/create schedules the latest **edit/delete using that existing ID**; it cannot acknowledge an obsolete revision as delivered.
5. A known-ID edit/delete failure uses the bounded three-attempt policy only for its claimed revision. Restart recovery preserves parked unknown first Creates but safely reclaims unrelated interrupted work.

## Versioned startup and channel claims

Startup recognizes released v0.1.0 schema at version 0 and validates the complete current schema at version 1. Empty files bootstrap current v1 atomically. The sole registered v0→v1 step reserves a dedicated connection, suspends FK enforcement before `BEGIN IMMEDIATE`, and rechecks header and inventory under the writer lock. All 30 released tables are staged in SQL TEMP before any DROP; the 45-table current ORM target is created once. Native INSERT/SELECT and JOIN build scoped locations and typed claims, followed by the planned one-time AP identity deltas. Published objects retain their source IDs; features absent from released v0 stay empty. No legacy schema inventory schema or internal version is created. Target definitions, every original row, `foreign_key_check` and `integrity_check` are verified before the sole version-1 stamp and commit. Errors restore original schema/data and header; FK enforcement is restored and verified in `finally`, with unsafe connections invalidated. Current v1 startup is read-only and never repairs a partial schema.

Subscribe resolves policy and performs gateway work outside the writer transaction. Persistence rechecks the typed channel owner and updates only the expected failed subscription in place; replacement or concurrent claims fail closed. Shared ActivityPub Follow state is global. Discord fanout selects accepted/active current primary claims, while history lookups use scoped copy locations independently of current ownership. Personal-feed move releases the old claim and claims the new location in one writer transaction.

During released identity cutover only relationships of renamed actors lose confirmation: changed communities lose incoming remote_subscribers; changed service/local target relations keep their Follow/channel rows as failed with cleared Follow IDs. Local subscribers and claims survive. Normal subscribe retry sends a new Follow after commit; migration performs no delivery itself.

## local subscriber synchronization live callback shutdown (partial)

Production composition now creates one `DiscordAdapter` borrowing `BridgeBot`.
`BridgeBot` callbacks enter its process-local admission gate before touching
shared persistence or the federation gateway, and unregister in `finally`.
When the bot stops, `app.py` closes admission, cancels/joins outstanding
Gateway callback tasks and stops the Personal Feed worker **inside** the
Discord SDK context. The SDK closes next; `Runtime.close()` subsequently closes
Fedify and the database. `BridgeBot.close()` must not close the shared gateway.
This is a callback-lifecycle change only: local subscriber synchronization's normalized-domain ingress
and transport cutover of community fanout are still in progress.

## local subscriber synchronization Local Community normalized source (pre-cutover)

1. `LocalCommunityRuntime.handle_normalized_thread_create()` checks the incoming
   `ThreadCreated` and actual starter `MessageView` as Discord forum inputs.
   The active host or local-subscriber forum, guild and community lifecycle
   are verified from SQLite, not selected from DTO metadata alone.
2. `LocalCommunityDiscordCreateHandler` publishes one AP post through the
   existing `ContentPublishService` and records the original host or subscriber
   physical source in `LocalCommunityThreadSurface`; the latter does not imply
   host ownership for a subscriber-authored post.
3. `LocalCommunityDiscordFanout.fanout_normalized_thread()` creates only missing
   physical copies through `PlatformDeliveryPort`, excluding the source forum.
   A repeated thread is checked against the original persisted published
   identity before any recovery fan-out; mirrored Discord threads never become
   a second AP post.
4. `handle_normalized_message_create()` checks the current source thread
   surface and uses the actual starter or mapped source-local parent to resolve
   the AP comment parent. A mapped parent or known starter in a different physical thread is
   rejected. Each subscriber/host destination resolves its own parent message
   ID; incomplete nested delivery does not fall back to an unrelated starter.
5. These methods are available to scenario tests but are **not** invoked by
   the production forum Gateway callbacks until the coordinated local subscriber synchronization cutover.

## Remote ActivityPub Update/Delete -> Discord copies (local subscriber synchronization Checkpoint 36)

1. The existing ActivityPub dispatcher resolves a persisted Remote Community
   post or comment group by the canonical AP object ID. Missing groups produce
   an unmapped result without Discord I/O.
2. For posts, every recorded thread delivery supplies its own physical starter
   message ID. For comments, only bot-authored `inbound` and `mirror` message
   deliveries are eligible; `source` rows remain untouched.
3. In production, `CommunityRuntime` passes the registered `DiscordAdapter` to
   `community_sync/edit_delete.py`. Each destination uses an exact
   connector-scoped `MessageRef` instead of `BridgeBot.get_thread_by_id()`.
4. A post Update replaces the starter content. A comment Update reads the
   exact message first and preserves its attribution header; mismatched or
   unavailable reads do not cause an unverified overwrite. Delete writes
   `*deleted by creator*`, preserving the physical conversation structure.
5. Failures are logged per target and do not suppress edits to healthy targets.
   Compatibility callers without an adapter still use the preexisting SDK path
   until its final removal during the local subscriber synchronization dependency audit.

## Local Community inbound ActivityPub Update/Delete (local subscriber synchronization)

1. `activitypub_handlers.py` resolves the accepted, authenticated inbound
   activity to `LocalCommunityRuntime`, which locates its canonical AP object.
2. `local_communities/mutations.py` chooses persisted thread or message surfaces;
   `LocalCommunitySurfaceFanout` checks per-forum metadata and guild policy.
3. With a registered adapter, `LocalCommunityDiscordFanout` mutates exact
   `MessageRef(discord-primary, thread_id, starter_or_comment_id)` copies through
   `PlatformAdapterPort`. Updates verify the read receipt before preserving the
   stored attribution header; Deletes edit bodies to a visible tombstone.
4. A failure of one Discord surface does not block the others. The existing
   `LocalCommunityFederationFanout` relays to eligible remote subscribers.
   SDK-backed Discord-originated mutations remain a separate pending cutover.
