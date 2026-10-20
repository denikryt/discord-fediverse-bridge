# Platform API (Stage 02 contract)

This document owns the SDK-independent transport boundary between Bridge Core and physical platforms. Stage 02 **defines** the contract and stateful test fakes; it does **not** activate a Discord adapter, a Fluxer connector, a second Gateway receiver, a new database migration or a new delivery queue. The deployed path still uses `BridgeBot`, `DiscordEventRouter` and existing fanout/Personal Feed workers directly; the exact temporary boundaries are marked `MULTIPLATFORM_STAGE02_TEMP` in `src/runtime_ports.py`, `src/discord_bot.py` and `src/app.py`.

**Stage 03 incremental activation:** production raw message edit/delete and raw thread update/delete callbacks pass through `DiscordAdapter` normalization and `PlatformEventIngress`. Bound text/announcement Personal Feed roots now use `MessageCreated` and the same sink; the publisher rechecks persisted ownership, retains the source `created_at`, and never receives a raw SDK object on this path. Manual Personal Feed discussion threads from the published root also use `ThreadCreated` and a checked root/channel mapping, without fetching arbitrary starter history. The adapter independently verifies and normalizes thread messages (`ingest_thread_message_created`), including an authoritative parent lookup on a cold SDK cache. Personal Feed discussion-thread replies now use that production typed path, an exact-root SQLite check, and the existing deterministic User Actor Create publisher. Registration notices are best-effort adapter sends with stable attempt keys. Subscribed and local-community forum starters/replies **still use legacy SDK-shaped domain handlers** until their separate typed cutover; forum Create submitted to the neutral route fails explicitly. A preparatory `ContentPublishService.publish_normalized_post_to_community()` accepts a verified forum `ThreadCreated` with an independently observed `MessageView` starter, checks separate thread/starter IDs and author provenance, and delegates to the **same** canonical AP render/persistence code as the old publisher. A companion `publish_normalized_comment_to_community()` validates forum reply containment (`reply_to` must reference the same physical thread), author registration and moderation before the same shared canonical comment persistence. For both typed paths, unregistered/banned/moderation-unavailable rejection notices use an adapter-only best-effort send with a stable attempt key. Neither method is yet wired to production Gateway callbacks or forum fanout; no second live publication route has been enabled. The existing `DiscordFanout` now also exposes preparatory neutral sibling thread/message delivery through `PlatformDeliveryPort`: each destination must have a current accepted SQLite subscription for the same community and allowed guild, physical writes carry stable per-target `CreateAttempt` keys, and returned thread/starter IDs remain distinct. For replies, per-target parent references are resolved by the owning runtime. The preparation now also includes `community_sync.reply_mapping.resolve_normalized_reply_context()`: it obtains canonical parent-group and sibling-reference IDs from existing SQLite delivery rows using `MessageCreated.reply_to`, verifies source thread membership in the same group, and refuses forged cross-thread references. Missing parent mappings preserve the existing intentional flat-send fallback. Legacy SDK reference resolution remains in place only for the currently active production route. The old SDK-backed fanout remains the sole active route until the forum runtime/ingress cutover; the typed fanout does not claim to implement durable Stage 04 receipt recovery. Root attachments are explicitly unsupported, not silently removed from the ActivityPub body. Uncached message deletion remains ID-only; physical thread rename/delete remains a no-op. Stage 03 is not complete.

## Ownership and identities

Stage 01's `PlatformConnector` and `ChannelSurface` rows are authoritative for connector metadata and physical channel claims. A `ConnectorRef(key, platform, instance_origin)` is only a non-secret process snapshot; Fluxer origins must be the canonical value stored by the repository. `PlatformAdapterRegistry` requires an explicit set of allowed connectors and registers one matching adapter per key. `get` and `get_for_channel` never fall back to Discord, bind channels, write mapping rows or close SDK clients. Composition owns each adapter's client lifecycle. Connector presence alone is **not** authorization or a running Fluxer Gateway.

A `ChannelRef` is physically identified by `(connector_key, external_channel_id)`: its `kind` (`forum`, `text`, `announcement`, `unknown`) and `external_guild_id` are metadata excluded from equality/hash. Confirmed metadata can enrich a reference without creating a new identity. `ThreadRef(channel, external_thread_id)` inherits the parent identity; a forum thread does not imply its starter-message ID. `MessageRef(connector_key, external_container_id, external_message_id)` uses the **immediate** message container: a thread for a reply/starter, a text/announcement channel for a root. It can be constructed from raw `MESSAGE_DELETE(id, channel_id)` without fetching the parent forum. External identifiers stay opaque strings; two connectors can legitimately use the same values.

`ChannelView` provides verified physical kind, `ThreadView.starter` is the **actual** starter `MessageRef` when observable and otherwise `None`, and `MessageView` can provide content for partial edit reconciliation. A lookup does not establish a `ChannelSurface` claim or authorize publishing. The stateful contract fake rejects a thread lookup or send when the requested parent channel differs from the stored physical thread parent, even if the thread ID itself exists. Known conflicting guild/kind claims must fail before a privileged operation; unknown metadata is not a permission grant. Core/persistence owns claim resolution, canonical ActivityPub object IDs, copies, source/mirror roles, policy checks and fanout.

## Ports and boundary validation

`src/runtime_ports.py` is the **only** core-facing protocol location. Structural `PlatformLookupPort` provides `fetch_channel`, `fetch_thread`, `fetch_message`, `check_permissions`, `capabilities`, and read-only `reconcile_create`. `PlatformDeliveryPort` provides `create_forum_thread`, `create_reply_thread`, `send_message`, `edit_message`, `delete_message`. `PlatformAdapterPort` combines them with immutable `.connector`; `PlatformInboundSinkPort` accepts one normalized event. The Stage 03 `src/discord_legacy_projection.py` is the temporary checked boundary for mapping `discord-primary` references to Discord-only integer repository IDs (including explicit thread-parent and message-container selection); this is not a global ID conversion for Fluxer or other connectors. No generic outbound physical thread edit/delete or channel-management operation exists in this stage. Incoming thread update/delete events remain supported.

Examples (SDK imports and integer conversion are deliberately absent):

```python
forum = ChannelRef('discord-primary', '900', 'forum', '1')
created = CreatedThread(
    ThreadRef(forum, '111'),
    MessageRef('discord-primary', '111', '222'),  # actual starter != thread id
    'bot',
)
reply = MessageRef('discord-primary', '111', '333')
raw_delete = MessageDeleted('discord-primary', reply)  # no parent lookup required
attempt = CreateAttempt('durable-copy-key', 'discord-primary', 'message', created.thread)
```

Before a create, the concrete adapter calls `validate_create_attempt(attempt, operation, destination)` and checks the verified channel kind, capability and current permissions **before network I/O**. `validate_message_reply` rejects a parent outside the exact connector and immediate container; the caller explicitly elects a flat send with `reply_to=None` when policy permits it. Successful create receipts are checked with `validate_created_receipt` before admission to core mapping storage. A forum receipt must not contradict the requested channel’s **known** kind or guild: `ChannelRef` equality intentionally compares physical IDs only, so receipt validation separately compares known metadata while allowing unknown values to be refined. A created forum post returns **both** real thread and starter message IDs; edits/deletes mutate physical message/starter copies only. The adapter rechecks permissions at the actual write, regardless of stale preflight results. Capability booleans are not per-account ACL outcomes; `PermissionResult` distinguishes `allowed`, `denied`, `unknown`, `unsupported`.

`ContentDocument` is neutral body/portable Markdown, optional title/link and `AttachmentRef` URL metadata, not an SDK File/native reply reference. The adapter renders links, mentions and supported attachments, handles platform size/tags/uploads, and disables unsolicited mention pings on mirrored content. `DeliveryPresentation('bot'|'webhook')` asks for one actual send mode; unsupported webhook mode must fail explicitly, never silently fall back to bot. Visual webhook labels are not ActivityPub author identities.

## Inbound Gateway event family

`ThreadCreated`, `ThreadUpdated`, `ThreadDeleted`, `MessageCreated`, `MessageUpdated`, `MessageDeleted` are frozen, connector-scoped platform events; they are **not** Fedify/ActivityPub events. Each has diagnostic, timezone-aware `received_at` and optional `external_guild_id`; the API rejects naive or non-datetime timestamps. `MessageUpdated.edited_at` and `MessageView.edited_at` require timezone-aware `datetime` when known, with `None` meaning an unknown edit time. Stage 03 extends `MessageCreated.created_at` to preserve the actual source message creation instant separately from diagnostic `received_at`; its optional absence keeps generic transport fakes valid, but Discord publication must obtain the source timestamp. `MessageUpdated.author` is optional best-effort `AuthorRef` metadata, rejected if its connector differs from the edited message; a missing author is not inferred from a raw cache-less edit. A resolved `thread_context` must share the message's immediate container, and any accompanying `parent_channel` must identify the same physical channel without contradicting its known kind/guild metadata. Event `external_guild_id` is checked against **both** available channel contexts, even when the first has unknown metadata. A reply reference may point into another container for Discord `THREAD_STARTER_MESSAGE`; this is not an outbound reply permission. Raw message updates/deletes require no parent channel, cache or network fetch. Missing `MessageUpdated.content is None` is **not** empty content; `ContentDocument(body='')` means explicit empty content. `ThreadCreated.newly_created=None` and `MessageCreated.is_starter=None` mean unknown, not false.

`AuthorRef.kind` distinguishes `human`, `bot`, `webhook`, `unknown`; `is_bridge_owned` may be asserted only for an authenticated owned bot/webhook, never from a display name or text marker. Neither provenance nor a Gateway event alone establishes a canonical source mutation. Stage 03 will translate Discord events once at ingress; Stage 04 must combine source/mirror mapping, policy, dedup, reconnect replay and out-of-order receipts without activating parallel raw/normalized callbacks.

**Stage 03 Checkpoint 29 (pre-cutover):** `CommunityRuntime.handle_normalized_thread_create()` and `handle_normalized_message_create()` now connect normalized forum publication with the existing canonical `CommunityThreadGroup`/`CommunityMessageGroup` SQLite mappings, `resolve_normalized_reply_context()` and typed sibling fan-out. Both require current accepted source subscription/guild policy and preserve distinct thread/starter IDs, echo suppression, nested-parent mapping and partial-target isolation. They are intentionally not yet dispatched from production Gateway callbacks; local-community normalized ownership and the coordinated cutover are outstanding.

## Unknown create results and reconciliation

All three create operations take a core-generated `CreateAttempt(key, connector_key, operation, destination)`. It identifies **one desired physical copy**, not each HTTP retry. Stage 04 will persist the key, the complete immutable request (including content/title/presentation/target), and delivery state **before** sending. Reusing a key with changed data is invalid. Adapter-local correlation must also reject altered requests it has seen. The DTO has no timestamp and no retry-deadline state.

`PlatformOperationError(reason='unknown_delivery')` is the **only** uncertain-create classification; other reasons are `not_found`, `forbidden`, `unsupported`, `invalid_request`, `rate_limited`, `unavailable`. A response may have been lost after the remote object was committed. `reconcile_create(attempt)` is a side-effect-free read that returns a verified `CreatedThread` (forum), `CreatedMessage` (message), `ThreadRef` (reply thread), or `None` (**unresolved**, not confirmed non-delivery). A known receipt must match connector, target and operation. For forum/message creates it additionally needs a provable link to the exact attempt; matching title/body/timestamps or a negative history scan is not proof. Discord message `nonce`/`enforce_nonce` is time-limited and does not give a documented safe retry deadline; forum creation has no matching documented uniqueness contract. `None` never triggers blind create replay. No Stage 02 fake claims to implement Stage 04's durable recovery workflow.

**Personal Feed exception:** Discord threads created *from* an existing text/announcement-channel root message share its ID; there can be only one. A verified existing `ThreadRef` with the root ID, correct parent connector/channel and compatible thread kind can be adopted even when it predates the attempt. This is proof of the intended **target**, not proof the current attempt created it. `create_reply_thread` may return such a preexisting thread rather than issuing a second create; `reconcile_create` may return it when verified with a network/platform lookup. The exception never extends to forum posts or arbitrary messages, nor does it authorize sending a reply without permissions. Other platforms must verify any equivalent guarantee rather than assuming Discord semantics.

## Activation boundaries and coverage

Stage 03 will wrap the existing Discord transport, translate the six events atomically at Gateway ingress, and keep text/announcement Personal Feed behavior. Stage 04 will implement durable delivery intents, claim/source ownership, cross-forum routing, dedup, target-specific parent lookup, verified receipt adoption, and unresolved deliveries. Stage 05 verifies Fluxer APIs/permissions/endpoint discovery against a real instance; Stage 06 connects heterogeneous communities. The old Discord ports are deliberately retained until their last live consumers are migrated.

Regression contracts: `tests/test_platform_types.py` for type/identity/admission semantics; `tests/behavior/test_platform_api_stage02.py` for realistic physical copy actions against stateful transport fakes and Stage 01 claims; existing Discord/Personal Feed/ActivityPub suites for *unchanged* live behavior. Stage 02 adds no SQL schema, ActivityPub URL change, SDK client, webhook worker or new background task.

References: [Python Protocol specification](https://typing.python.org/en/latest/spec/protocol.html); [Discord Gateway events](https://github.com/discord/discord-api-docs/blob/main/developers/events/gateway-events.mdx); [Discord Channel API (forum creation, thread from message)](https://github.com/discord/discord-api-docs/blob/main/developers/resources/channel.mdx); [Discord Message API (nonce, allowed mentions)](https://github.com/discord/discord-api-docs/blob/main/developers/resources/message.mdx); [ActivityPub W3C](https://www.w3.org/TR/activitypub/).

## Stage 03 transport cutover (in progress)

`src/discord_adapter.py` implements the physical Discord side of this contract.
A borrowed SDK client resolves exact channels, parent-validated threads and
messages, and checks current bot permissions before writes. Forum creation
uses separate observed thread and starter message IDs; message sends suppress
mentions. Webhook presentation and attachment uploads are explicitly refused,
not silently replaced by bot/plain-text sends.

Each connector-scoped `CreateAttempt` key is admitted through a process-local
singleflight ledger. Matching concurrent calls share one receipt; a changed
request is rejected, an uncertain result is retained without physical replay,
and receipt capacity fails closed rather than evicting evidence. This registry
is not a durable retry queue or a cross-restart deduplicator; that remains the
Stage 04 boundary. Routing the existing domain runtimes through the adapter is
not yet complete.

## Stage 03 Checkpoint 29 — typed remote-community orchestration (pre-cutover)

`CommunityRuntime` now has typed forum starter/reply paths using real SQLite group/delivery receipts, checked subscriber/guild policy, the existing publisher and neutral sibling fan-out. Eight new end-to-end SQLite scenarios and 47 related focused regressions passed with an import-only `discord` stub. Production forum callbacks and local-community routes are still SDK-based and have not been switched; no parallel live publisher was added. Full pytest and installed Fedify/Discord integration remain blocked by unavailable dependencies.

## Stage 03 Checkpoint 30 — local-community physical adapter fan-out (pre-cutover)

The existing `LocalCommunityDiscordFanout` now has typed transport methods that preserve per-recipient parent IDs, active subscriber/host selection, SQLite surface idempotency and per-target error isolation without making SDK writes. Nine focused SQLite tests pass. No production event route has been switched. Full dependency-backed validation remains incomplete (`hypothesis`, `discord.py` and Node Fedify unavailable); two SDK-dependent legacy inbound test cases could not load under the import-only Discord shim, not an observed implementation regression.

**Stage 03 Checkpoint 31 — Local Community normalized source (pre-cutover):**
`LocalCommunityRuntime` now routes typed host and active local-subscriber
starters/replies through existing `ContentPublishService` and SQLite canonical
surfaces and uses the typed physical fanout. Duplicate recovery verifies the
published source message/forum, author, original body and title; matching
subscriber roles alone do not identify the original, as mirrored subscriber
copies have the same role. Nested reply parents cannot borrow another source
thread's persisted message surface or known starter ID. Production forum Gateway callbacks remain
on the prior SDK handler pending coordinated cutover; full SDK-dependent and
Fedify-native security integration remains blocked by missing dependencies.

## 2026-10-19 — Stage 03 Checkpoint 32 forum Gateway cutover

- Verified in focused SQLite/fake SDK scenarios: forum Create now uses one
  normalized ingress; concurrent same-thread callbacks are serialized and
  remote/local source identity and replay are checked by
  their existing runtimes and persisted mappings. Exact starter missing/foreign
  must not trigger publication; an unsupported attached starter is rejected.
- Outstanding release verification: pinned `discord.py` runtime and full pytest
  remain unavailable; native Fedify transport/SSRF acceptance is also pending.
- Remaining migration: ActivityPub inbound physical SDK fan-out, mutations,
  User Actor SDK reads and command lifecycle audit. No Stage 04 outbox added.

## 2026-10-19 — Stage 03 Checkpoint 33 remote inbound post transport

- Remote ActivityPub post creation now uses typed `PlatformDeliveryPort` in the live dispatcher when `Runtime.discord_adapter` is present; original legacy direct-entry tests retain their old SDK path until shared inbound comment/backfill and SDK dependency cleanup are complete.
- Exact per-target thread/starter receipts, checked guild claims and active subscriptions are regression-tested with real SQLite. Duplicate AP post events make no additional typed create call.
- Existing shared `CommunityThreadGroup` dedup still prevents a failed/uncertain partially delivered inbound post from automatically recovering a missing target after a crash. Durable per-target recovery belongs to Stage 04; no blind second POST was introduced.
- Full SDK-backed and Fedify-native integration remains blocked by unavailable dependencies.

## 2026-10-19 — Stage 03 Checkpoint 34 mapped inbound comment transport

- `CommunityRuntime.handle_normalized_inbound_comment()` now has a typed SDK-free delivery path for already fully mapped Remote Community post/thread groups. Real SQLite tests verify per-recipient reply references and reject foreign canonical parent/foreign physical receipt, revoked subscription and missing mapping.
- This method is **not** wired into the ActivityPub HTTP dispatcher. An out-of-order comment still requires existing remote post backfill; activating the typed method globally before its migration would reject otherwise recoverable events.
- Remaining SDK release blockers and durable uncertain-creation recovery are unchanged from Checkpoint 33.

## 2026-10-19 — Stage 03 Checkpoint 35 remote inbound comment backfill

- Production Remote Community comment delivery now selects the typed adapter path if present; this path invokes the typed parent-post backfill first for missing/partial canonical post mappings. Recovery preserves previously confirmed forum/thread/starter receipts and creates only currently authorized missing deliveries. Incorrect fetched object identity or a bad physical receipt cannot permit child message delivery.
- Existing direct SDK compatibility entry points remain until final dependency cleanup. Full pytest is blocked by missing `hypothesis`; real Discord/Fedify behavior is not yet exercised.
- **Open release blocker:** the inherited Python `_fetch_ap_object` still does a direct `aiohttp` GET and lacks complete DNS pinning/redirect SSRF verification. The implementation reuses this existing fetcher; it is not evidence of a secure outgoing GET. Finish R7-F2 before release.
- **Open Stage 04 limitation:** durable reconciliation of a Create that may have completed physically without a confirmed receipt remains out of scope. Do not blindly resend an uncertain Discord Create after restart.

## 2026-10-19 — Stage 03 Checkpoint 36 Remote Community AP mutations

- Production inbound Remote Community post/comment Update/Delete now edits exact persisted physical Discord message IDs through `Runtime.discord_adapter`. Comment Updates verify the current message and preserve its attribution header; a bad read or individual target failure does not overwrite another message or stop sibling edits. Real SQLite scenario tests cover these boundaries.
- Legacy direct-call SDK mutation compatibility remains while the rest of the Stage 03 dependency audit proceeds. Local Community inbound mutations, User Actor, native Fedify SSRF validation and the Python backfill GET release blocker remain open.

- Stage 03 Checkpoint 37: inbound Local Community AP Update/Delete now use
  exact scoped DiscordAdapter edits when registered. SDK-based local
  Discord-originated mutation fanout still awaits its own cutover; physical
  mutation failure remains best-effort without Stage 04 durable reconciliation.

- Stage 03 Checkpoint 39: Local Community persisted mutation surface receipts were insufficient for authorization after a subscriber was removed. Fixed by verifying current host/subscriber ownership and policy at each physical edit/delete boundary. Real SQLite regressions cover host/subscriber origins, revoked subscribers and transport failures; full SDK integration remains unverified.

- **Stage 03 Checkpoint 40:** User Actor owner DM and partial Discord mutation reconciliation now use the live DiscordAdapter, not BridgeBot's direct SDK methods. At-most-once notification claim and durable reconciliation remain in existing services; file-backed SQLite tests cover response loss, verified missing, wrong receipt and transient GET. Real discord.py integration still required before Stage 03 release.

- **Stage 03 Checkpoint 41 (security hardening):** Python AP parent backfill no longer performs unrestricted aiohttp GET. `activitypub_safe_fetch` requires public HTTPS DNS/socket, disables redirects/proxies, bounds JSON; isolated 16-case security regression suite passed. Complete end-to-end Fedify/Discord dial-level validation is still open.
- **Stage 03 Checkpoint 41 (dependency audit):** core `CommunityRuntime` and shared `content_sync` can be imported without `discord.py`; remaining legacy physical SDK call paths in `community_sync/discord_fanout.py`, `local_communities/inbound_create.py`, `local_communities/discord_fanout.py`, `content_sync/edit_delete.py`, and raw reference construction remain to be removed once all live consumers are cut over.

- **Stage 03 Checkpoint 42:** Production Local Community AP→Discord host post,
  host comment and local-subscriber fan-out now use the registered `DiscordAdapter`
  instead of direct SDK writes. File-backed SQLite regressions cover author
  admission, physical receipts, duplicate replay, nested per-destination
  parents, failed targets, Follow revocation and host rebinding. The unbound
  legacy SDK branch is still present for direct-call tests; removal and
  independent integration validation remain open.
- **Stage 03 release blockers:** The Fedify lockfile still pins the historical
  vulnerable version, native `sendActivity()` SSRF and remote signed activity
  ingress are not verified with installed dependencies, and the full pytest
  suite cannot collect without `hypothesis`/`discord.py` in this environment.

- **Stage 03 Checkpoint 43:** Corrected adapter-bound Local Community inbound
  comment admission so foreign/missing nested parents, cross-community post
  ownership and stale host surfaces do not leave an inbound conversation row
  before rejection. Physical host Create receipts now validate request/mode
  and require affirmative forum/guild metadata; unverified possibly sent
  outcomes remain `unknown_delivery`. Real SQLite tests cover successful root
  and nested replies, missing-parent eventual recovery, forged modes and
  stale/cross-owner refusals. Legacy unbound SDK paths, unavailable full-suite
  dependencies and pinned Fedify upgrade remain open.

- **Stage 03 Checkpoint 44:** Local Community subscriber forum and comment
  Create receipts now verify the full typed attempt, requested bot/webhook
  presentation and (for forums) affirmatively known guild/kind before SQLite
  mapping. Fake-network/real-SQLite tests confirmed a forged webhook receipt
  previously persisted as delivered; after correction it fails independently
  while a healthy sibling still succeeds. An unverified possibly transmitted
  physical create is not automatically retried or adopted after restart;
  Stage 04 remains responsible for durable unknown-create reconciliation.

- **Stage 03 Checkpoint 45:** Adapter-backed Remote Community sibling forum
  and comment receipts now validate `CreateAttempt`, bot delivery mode and
  (for forums) affirmative kind/guild before writing group delivery rows.
  Real SQLite runtime regressions demonstrated that previously accepted wrong-
  mode webhook receipts would persist as mirrors; now only healthy targets are
  recorded. Uncertain physical creates, the remaining SDK-only entry points,
  Fedify security upgrade and full dependency-backed integration remain open.

- **Stage 03 Checkpoint 46:** Production Discord connector selection now reads
  the exact migrated SQLite row and refuses missing, non-Discord or origin-bearing
  `discord-primary` metadata before adapter registration. `Runtime.close()`
  attempts all ordered resource releases even after one shutdown exception;
  a second concurrent cancellation and command-task lifetime still need
  SDK-backed integration checks. T5 thread-mutation no-op is now explicitly
  marked. Fedify pinned SDK and fan-out partial-target recovery remain open.

- **Stage 03 Checkpoint 47:** Remote Community AP→Discord post/comment fan-out
  now continues to other accepted targets after a failed physical create and
  preserves confirmed per-target mappings. If every target fails, the handler
  still propagates the original error; ordinary duplicates never replay
  uncertain Discord Create. Explicit verified parent backfill may recover
  a missing post target using its existing canonical group. Automatic
  durable comment/unknown-create recovery remains Stage 04; native Fedify
  security gates are still release-blocked.

- **Stage 03 Checkpoint 48:** DiscordAdapter classifies explicit HTTP
  Forbidden/NotFound/429 Create responses as definite non-delivery, permitting
  a later policy-directed retry with the same key. Network timeout and
  unclassified errors remain `unknown_delivery` and sticky to prevent blind
  duplicate POSTs; physical receipt/restart uncertainty remains Stage 04.
  Integration against actual pinned discord.py exceptions is still outstanding.

- **Stage 03 Checkpoint 49:** A malformed typed Create receipt returned
  *after* a physical SDK create is now a sticky `unknown_delivery` in
  `DiscordAdapter`'s per-key coordinator, even if the receipt validator raises
  `invalid_request`. Previously the key was removed and the same physical
  Create could be issued twice. Explicit pre-send 403/404/429 failures remain
  retryable; in-process verified receipts remain reusable. This guard is not a
  cross-restart durable intent ledger (Stage 04).
