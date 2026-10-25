# Platform API

This document owns the SDK-independent transport boundary between Bridge Core and physical platforms. The Platform API **defines** the contract and stateful test fakes; it does **not** activate a Discord adapter, a Fluxer connector, a second Gateway receiver, a new database migration or a new delivery queue. The deployed path still uses `BridgeBot`, `DiscordEventRouter` and existing fanout/Personal Feed workers directly; the exact temporary boundaries are marked `MULTIPLATFORM_LEGACY_DISCORD_PORT_TEMP` in `src/runtime_ports.py`, `src/discord_bot.py` and `src/app.py`.

**Discord adapter integration:** production raw message edit/delete and raw thread update/delete callbacks pass through `DiscordAdapter` normalization and `PlatformEventIngress`. Bound text/announcement Personal Feed roots now use `MessageCreated` and the same sink; the publisher rechecks persisted ownership, retains the source `created_at`, and never receives a raw SDK object on this path. Manual Personal Feed discussion threads from the published root also use `ThreadCreated` and a checked root/channel mapping, without fetching arbitrary starter history. The adapter independently verifies and normalizes thread messages (`ingest_thread_message_created`), including an authoritative parent lookup on a cold SDK cache. Personal Feed discussion-thread replies now use that production typed path, an exact-root SQLite check, and the existing deterministic User Actor Create publisher. Registration notices are best-effort adapter sends with stable attempt keys. Subscribed remote and local-community forum starters/replies now use the single typed `PlatformEventIngress` route. The adapter resolves an observed cached starter or fetches only the exact root message; it never chooses an arbitrary history entry. Physical source and subscription checks remain owned by the canonical runtimes. A preparatory `ContentPublishService.publish_normalized_post_to_community()` accepts a verified forum `ThreadCreated` with an independently observed `MessageView` starter, checks separate thread/starter IDs and author provenance, and delegates to the **same** canonical AP render/persistence code as the old publisher. A companion `publish_normalized_comment_to_community()` validates forum reply containment (`reply_to` must reference the same physical thread), author registration and moderation before the same shared canonical comment persistence. For both typed paths, unregistered/banned/moderation-unavailable rejection notices use an adapter-only best-effort send with a stable attempt key. Both methods are wired to the forum production Gateway callbacks; no second SDK-based Create callback publishes the same event. The existing `DiscordFanout` now also exposes preparatory neutral sibling thread/message delivery through `PlatformDeliveryPort`: each destination must have a current accepted SQLite subscription for the same community and allowed guild, physical writes carry stable per-target `CreateAttempt` keys, and returned thread/starter IDs remain distinct. For replies, per-target parent references are resolved by the owning runtime. The preparation now also includes `community_sync.reply_mapping.resolve_normalized_reply_context()`: it obtains canonical parent-group and sibling-reference IDs from existing SQLite delivery rows using `MessageCreated.reply_to`, verifies source thread membership in the same group, and refuses forged cross-thread references. Missing parent mappings preserve the existing intentional flat-send fallback. Legacy SDK reference resolution remains only for separate inbound ActivityPub physical paths awaiting migration; the typed fanout does not claim to implement durable durable fan-out receipt recovery. Root attachments are explicitly unsupported, not silently removed from the ActivityPub body. Uncached message deletion remains ID-only; physical thread rename/delete remains a no-op. Discord adapter integration is not complete.


**Discord adapter integration Checkpoint 30 Local Community preparation:** `LocalCommunityDiscordFanout.fanout_normalized_thread()` and `.fanout_normalized_message()` now create physical host/local-subscriber copies through `PlatformDeliveryPort` without SDK writes. The same active-subscriber and guild-policy target selection and existing `LocalCommunityThreadSurface`/`LocalCommunityMessageSurface` persistence remain authoritative. Stable per-canonical-target `CreateAttempt` keys are used, returned forum/guild/thread/starter identities are checked, and each reply resolves the parent message ID on the destination surface; an unrepresented nested parent is skipped rather than flattened. A failed or invalid target receipt does not prevent another target. These methods are **pre-cutover** and not connected to the production Local Community handlers yet. They do not supply durable fan-out durable receipt reconciliation.

**Discord adapter integration Checkpoint 39 — Discord-originated Local Community mutations:** production `LocalCommunityRuntime` binds the single `DiscordAdapter` during `app.py` composition; `LocalCommunityMutationHandler` retains canonical AP Update/Delete responsibility and supplies `PlatformAdapterPort` to the existing surface fanout for starter/comment edits and visible tombstones. Each physical target must still belong to the current host placement or the exact active local subscriber and pass guild policy; raw historical surface IDs never imply current ownership. A fetched `MessageView` must match the requested scoped `MessageRef` before an edit. Failed edits do not use direct SDK fallback. Source copies remain untouched and independent targets continue after transport failures. Direct-call compatibility without adapter remains temporary until final dependency removal.

**Discord adapter integration Checkpoint 38 — Discord-originated Remote Community mutations:** in production `DiscordFanout` receives the bound `DiscordAdapter` and performs mirror message edit/delete using `PlatformAdapterPort.fetch_message/edit_message/delete_message`, not a direct SDK object. The canonical runtime still owns source-vs-mirror selection and ActivityPub Update/Delete. Before each physical mutation, the fanout checks the current accepted subscription, matching community actor and guild permission. Edit uses an exact scoped `MessageRef` read to preserve the attribution header, then records only a successful mutation for Gateway echo suppression. Individual failed targets cannot trigger SDK fallback or abort other copies. Compatibility callers without the adapter still use the prior SDK path until the remaining Discord adapter integration legacy branches are removed.

**Discord adapter integration startup connector guard (Checkpoint 46):** after SQLite migration, `PlatformSurfaceRepository.get_discord_primary_connector()` reads the stored `discord-primary` identity; missing or incompatible platform/origin metadata fails startup before Discord Gateway login. `Runtime` retains the exact `PlatformAdapterRegistry` that registered the one adapter. No connector is provisioned or silently claimed by adapter startup.

## Ownership and identities

connector-scoped persistence's `PlatformConnector` and `ChannelSurface` rows are authoritative for connector metadata and physical channel claims. A `ConnectorRef(key, platform, instance_origin)` is only a non-secret process snapshot; Fluxer origins must be the canonical value stored by the repository. `PlatformAdapterRegistry` requires an explicit set of allowed connectors and registers one matching adapter per key. `get` and `get_for_channel` never fall back to Discord, bind channels, write mapping rows or close SDK clients. Composition owns each adapter's client lifecycle. Connector presence alone is **not** authorization or a running Fluxer Gateway.

A `ChannelRef` is physically identified by `(connector_key, external_channel_id)`: its `kind` (`forum`, `text`, `announcement`, `unknown`) and `external_guild_id` are metadata excluded from equality/hash. Confirmed metadata can enrich a reference without creating a new identity. `ThreadRef(channel, external_thread_id)` inherits the parent identity; a forum thread does not imply its starter-message ID. `MessageRef(connector_key, external_container_id, external_message_id)` uses the **immediate** message container: a thread for a reply/starter, a text/announcement channel for a root. It can be constructed from raw `MESSAGE_DELETE(id, channel_id)` without fetching the parent forum. External identifiers stay opaque strings; two connectors can legitimately use the same values.

`ChannelView` provides verified physical kind, `ThreadView.starter` is the **actual** starter `MessageRef` when observable and otherwise `None`, and `MessageView` can provide content for partial edit reconciliation. A lookup does not establish a `ChannelSurface` claim or authorize publishing. The stateful contract fake rejects a thread lookup or send when the requested parent channel differs from the stored physical thread parent, even if the thread ID itself exists. Known conflicting guild/kind claims must fail before a privileged operation; unknown metadata is not a permission grant. Core/persistence owns claim resolution, canonical ActivityPub object IDs, copies, source/mirror roles, policy checks and fanout.

## Ports and boundary validation

`src/runtime_ports.py` is the **only** core-facing protocol location. Structural `PlatformLookupPort` provides `fetch_channel`, `fetch_thread`, `fetch_message`, `check_permissions`, `capabilities`, and read-only `reconcile_create`. `PlatformDeliveryPort` provides `create_forum_thread`, `create_reply_thread`, `send_message`, `edit_message`, `delete_message`. `PlatformAdapterPort` combines them with immutable `.connector`; `PlatformInboundSinkPort` accepts one normalized event. The Discord adapter integration `src/discord_legacy_projection.py` provides temporary checked conversion for mapping `discord-primary` references to Discord-only integer repository IDs (including explicit thread-parent and message-container selection); this is not a global ID conversion for Fluxer or other connectors. No generic outbound physical thread edit/delete or channel-management operation is provided. Incoming thread update/delete events remain supported.

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

**Discord adapter integration Checkpoint 48 SDK error policy:** Discord `Forbidden`/`NotFound` and an observable HTTP 429 from the actual Create operation are classified as definite rejected requests; their process-local CreateAttempt entry is released for a later policy-directed retry. A socket failure, timeout, cancellation or unverified receipt remains sticky `unknown_delivery`, with no second POST for that attempt key. This rule applies to forum thread, root-anchored reply thread and message creation; it does not guarantee server-side exactly-once delivery.

`ContentDocument` is neutral body/portable Markdown, optional title/link and `AttachmentRef` URL metadata, not an SDK File/native reply reference. The adapter renders links, mentions and supported attachments, handles platform size/tags/uploads, and disables unsolicited mention pings on mirrored content. `DeliveryPresentation('bot'|'webhook')` asks for one actual send mode; unsupported webhook mode must fail explicitly, never silently fall back to bot. Visual webhook labels are not ActivityPub author identities.

## Inbound Gateway event family

`ThreadCreated`, `ThreadUpdated`, `ThreadDeleted`, `MessageCreated`, `MessageUpdated`, `MessageDeleted` are frozen, connector-scoped platform events; they are **not** Fedify/ActivityPub events. Each has diagnostic, timezone-aware `received_at` and optional `external_guild_id`; the API rejects naive or non-datetime timestamps. `MessageUpdated.edited_at` and `MessageView.edited_at` require timezone-aware `datetime` when known, with `None` meaning an unknown edit time. Discord adapter integration extends `MessageCreated.created_at` to preserve the actual source message creation instant separately from diagnostic `received_at`; its optional absence keeps generic transport fakes valid, but Discord publication must obtain the source timestamp. `MessageUpdated.author` is optional best-effort `AuthorRef` metadata, rejected if its connector differs from the edited message; a missing author is not inferred from a raw cache-less edit. A resolved `thread_context` must share the message's immediate container, and any accompanying `parent_channel` must identify the same physical channel without contradicting its known kind/guild metadata. Event `external_guild_id` is checked against **both** available channel contexts, even when the first has unknown metadata. A reply reference may point into another container for Discord `THREAD_STARTER_MESSAGE`; this is not an outbound reply permission. Raw message updates/deletes require no parent channel, cache or network fetch. Missing `MessageUpdated.content is None` is **not** empty content; `ContentDocument(body='')` means explicit empty content. `ThreadCreated.newly_created=None` and `MessageCreated.is_starter=None` mean unknown, not false.

`AuthorRef.kind` distinguishes `human`, `bot`, `webhook`, `unknown`; `is_bridge_owned` may be asserted only for an authenticated owned bot/webhook, never from a display name or text marker. Neither provenance nor a Gateway event alone establishes a canonical source mutation. Discord adapter integration will translate Discord events once at ingress; durable fan-out must combine source/mirror mapping, policy, dedup, reconnect replay and out-of-order receipts without activating parallel raw/normalized callbacks.

**Discord adapter integration Checkpoint 29 (pre-cutover):** `CommunityRuntime.handle_normalized_thread_create()` and `handle_normalized_message_create()` now connect normalized forum publication with the existing canonical `CommunityThreadGroup`/`CommunityMessageGroup` SQLite mappings, `resolve_normalized_reply_context()` and typed sibling fan-out. Both require current accepted source subscription/guild policy and preserve distinct thread/starter IDs, echo suppression, nested-parent mapping and partial-target isolation. Both runtimes now receive forum events from the single production normalized ingress; Local Community ownership takes precedence over Remote Community on an overlapping forum claim.

## Discord-only User Actor auxiliary transport (Discord adapter integration Checkpoint 40)

`UserActorFollowService` sends one registered-owner notification through `DiscordAdapter.send_owner_notice()`, after the existing durable at-most-once notification claim. This operation is deliberately **not** part of `PlatformDeliveryPort`: a Discord user ID is the current destination until personal feed publication account linking. The adapter resolves the exact Discord user, disables mentions, and never automatically repeats a possibly delivered DM.

`UserActorDeliveryService` uses `DiscordAdapter.fetch_message_state(channel_id, message_id)` for its existing SQLite-backed partial-update reconciliation. The adapter validates the returned physical message/container identity and gives core an immutable SDK-free body/edited-time snapshot or a verified missing result. Transport failures and unverified receipts remain retryable reconciliation failures rather than being treated as deletion. These auxiliary methods use the same SDK admission/drain as normal adapter operations.

## Unknown create results and reconciliation

All three create operations take a core-generated `CreateAttempt(key, connector_key, operation, destination)`. It identifies **one desired physical copy**, not each HTTP retry. durable fan-out will persist the key, the complete immutable request (including content/title/presentation/target), and delivery state **before** sending. Reusing a key with changed data is invalid. Adapter-local correlation must also reject altered requests it has seen. The DTO has no timestamp and no retry-deadline state.

`PlatformOperationError(reason='unknown_delivery')` is the **only** uncertain-create classification; other reasons are `not_found`, `forbidden`, `unsupported`, `invalid_request`, `rate_limited`, `unavailable`. A response may have been lost after the remote object was committed. `reconcile_create(attempt)` is a side-effect-free read that returns a verified `CreatedThread` (forum), `CreatedMessage` (message), `ThreadRef` (reply thread), or `None` (**unresolved**, not confirmed non-delivery). A known receipt must match connector, target and operation. For forum/message creates it additionally needs a provable link to the exact attempt; matching title/body/timestamps or a negative history scan is not proof. Discord message `nonce`/`enforce_nonce` is time-limited and does not give a documented safe retry deadline; forum creation has no matching documented uniqueness contract. `None` never triggers blind create replay. No Platform API contract fake claims to implement durable fan-out's durable recovery workflow.

**Personal Feed exception:** Discord threads created *from* an existing text/announcement-channel root message share its ID; there can be only one. A verified existing `ThreadRef` with the root ID, correct parent connector/channel and compatible thread kind can be adopted even when it predates the attempt. This is proof of the intended **target**, not proof the current attempt created it. `create_reply_thread` may return such a preexisting thread rather than issuing a second create; `reconcile_create` may return it when verified with a network/platform lookup. The exception never extends to forum posts or arbitrary messages, nor does it authorize sending a reply without permissions. Other platforms must verify any equivalent guarantee rather than assuming Discord semantics.

## Stage 04 durable physical Create dispatch and cache ACK

`CreateAttempt.key` beginning with `fanout:v1:` is reserved for the SQLite-backed
`PlatformFanoutCoordinator`. After a committed intent `pending → dispatching`
claim, the intent repository first verifies that this exact claim remains
`dispatching` with its original immutable DTO and a live canonical/target
owner, then issues a process-local, sealed `CommittedCreateDispatchProof`.
The adapter requires this proof to mint a one-use `CoordinatorDispatchPermit`
bound to the concrete registered adapter, connector, physical request, intent
ID, fingerprint and dispatch token. Reusing that same live proof to mint
multiple permits is rejected; a claim that has already become `unresolved`
or `delivered` cannot produce a new proof. `DiscordAdapter` refuses reserved-key Create without a
matching issued permit **before consulting its singleflight registry**; a raw
caller cannot replay even after an ACK or adapter restart. Nonreserved direct
Create callers retain their existing process-local fail-closed cache.

Only the existing `PlatformDeliveryIntentRepository` may issue a
`CommittedCreateAckProof` after verifying the matching durable `delivered` or
`unresolved` terminal row. The adapter accepts that proof only for the exact
completed physical future and its original one-use permit. A verified success
may release a completed `delivered` flight; a lost response may release a
completed `unknown_delivery` flight **without asserting physical success**.
Neither a transport error alone, a fabricated receipt nor expiry of a
`dispatching` lease authorizes a second automatic POST. When the adapter
retains a completed **positive** result but the receipt/mapping SQLite commit
fails, the intent remains `unresolved`: `reconcile_unknown()` obtains a sealed
receipt/attempt/generation proof from that **same original transport flight**,
checks it again in the writer transaction, then adopts the original receipt
without another POST. An unknown SDK result, a restarted adapter with no
original positive flight, or a merely similar historical message has no such
proof and stays unresolved. This is not general cross-restart forum lookup.
A verified orphan receipt on `cancelled_after_send` is exposed by the intent repository's
read-only `inspect_orphans()` and logged during startup recovery. An operator
can record an audited `retained` decision; no automatic cleanup, synthetic
`removed_verified`, or forum-thread deletion is available through the
message-only typed port. Full deletion requires a separate authorized platform
admin procedure with independent thread-level proof. Reconciliation of
unknown Discord forum/message Creates remains read-only and cannot guess from
message history. No extra durable queue or adapter-owned SQLite connection is
introduced.

## Activation boundaries and coverage

Discord adapter integration will wrap the existing Discord transport, translate the six events atomically at Gateway ingress, and keep text/announcement Personal Feed behavior. durable fan-out will implement durable delivery intents, claim/source ownership, cross-forum routing, dedup, target-specific parent lookup, verified receipt adoption, and unresolved deliveries. participant mutation support verifies Fluxer APIs/permissions/endpoint discovery against a real instance; personal feed control plane connects heterogeneous communities. The old Discord ports are deliberately retained until their last live consumers are migrated.

Regression contracts: `tests/test_platform_types.py` for type/identity/admission semantics; `tests/behavior/test_platform_api_contract.py` for realistic physical copy actions against stateful transport fakes and connector-scoped persistence claims; existing Discord/Personal Feed/ActivityPub suites for *unchanged* live behavior. Platform API contract adds no SQL schema, ActivityPub URL change, SDK client, webhook worker or new background task.

References: [Python Protocol specification](https://typing.python.org/en/latest/spec/protocol.html); [Discord Gateway events](https://github.com/discord/discord-api-docs/blob/main/developers/events/gateway-events.mdx); [Discord Channel API (forum creation, thread from message)](https://github.com/discord/discord-api-docs/blob/main/developers/resources/channel.mdx); [Discord Message API (nonce, allowed mentions)](https://github.com/discord/discord-api-docs/blob/main/developers/resources/message.mdx); [ActivityPub W3C](https://www.w3.org/TR/activitypub/).

## Discord adapter integration transport cutover (in progress)

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
durable fan-out boundary. Routing the existing domain runtimes through the adapter is
not yet complete.

## Discord adapter integration Checkpoint 32 — production forum Create cutover

`BridgeBot.on_thread_create` and forum-thread `on_message` use only the adapter
`ThreadCreated`/`MessageCreated` ingress. The adapter rejects wrong physical
parent/guild, bot/webhook starter, unsupported attachments, and a missing or
foreign starter; without exact starter evidence no post is created. The single
router sends authorized forum sources to Local or Remote Community runtime,
which independently rechecks subscription, role and canonical ownership and
uses persisted SQLite dedup rather than a volatile bot seen-set. This switches
only **Gateway-originated forum Create**; ActivityPub inbound SDK-bound physical
deliveries and some edit/delete paths remain pending later Discord adapter integration work.

**Discord adapter integration Checkpoint 42 — Local Community incoming ActivityPub Create:**
`LocalCommunityRuntime.bind_discord_adapter()` now also binds the physical
`LocalCommunityInboundCreateHandler`. Incoming AP host posts and replies use
`PlatformDeliveryPort.create_forum_thread`/`send_message` with stable
`CreateAttempt` identities, source and current-host admission, and checked
physical receipts; accepted duplicate events use the same typed subscriber
fan-out without repeating confirmed copies. A late Undo(Follow) suppresses
remaining sibling/federation side effects, and a host placement change during
HTTP prevents adoption of the old receipt. Production composition already
registers this adapter. SDK-shaped compatibility entry points for unbound
legacy tests and durable fan-out unknown-create reconciliation remain unfinished.

**Discord adapter integration Checkpoint 43 — Local Community inbound reply admission and receipt hardening:**
The adapter-bound AP→Discord handler validates that an inbound comment belongs
to the current local community and that its nested parent has a physical
*host* message surface inside that same canonical thread **before** retaining
the canonical inbound conversation object. Missing nested parents are safely
skipped rather than delivered as flat replies, so they can be admitted once
the authoritative parent mapping arrives. A host surface bound to an obsolete
forum cannot admit a new comment. Both host thread and message Create receipts
are validated against their stable `CreateAttempt` and bot presentation; host
thread receipts additionally require positively observed forum/guild metadata,
not just absence of contradictions. An unverified receipt after a possible HTTP
send is classified `unknown_delivery` and is not mapped or automatically
resent. Existing SDK-only direct-call compatibility and durable fan-out durable
receipt recovery remain unfinished; no new schema or adapter API was added.

**Discord adapter integration Checkpoint 44 — Local Community sibling receipt admission:**
Every adapter-backed local-subscriber forum thread or reply receipt is checked
against the original `CreateAttempt` and requested bot presentation **before**
persisting its per-recipient SQLite surface. A forum thread receipt must also
positively identify the expected forum kind and guild. A mismatched reply mode
or physical destination may indicate an already transmitted create, so that
target fails as `unknown_delivery` without claiming a confirmed copy; healthy
sibling targets proceed independently. This is the existing Discord adapter integration typed
transport contract, not a durable uncertain-delivery recovery mechanism.

**Discord adapter integration Checkpoint 53 — verified Personal Feed root-thread adoption:**
`DiscordAdapter.create_reply_thread()` first reads the exact root message and
checks for a root-anchored thread with that same message ID under the verified
text/announcement channel and guild. An already-existing thread is adopted
without a second physical Create. If a create's response is lost, only this
independently verified unique root/thread mapping can resolve the attempt;
otherwise uncertainty remains sticky. `reconcile_create()` can repeat the
read-only verification after process restart but refuses a changed destination
under an already remembered attempt key. An unavailable lookup or mismatched
thread is not proof of non-delivery and cannot authorize another POST. This
exception does not apply to forum threads or arbitrary message sends; their
cross-restart recovery remains durable fan-out.


**Discord adapter integration Checkpoint 54 — root-thread retry and adoption corrections:**
A transient preflight channel/root/thread read that fails *before* a physical
Create does not permanently reserve the `CreateAttempt` key. Concurrent
waiters still receive that failed attempt's outcome; a subsequent caller can
retry safely. After a potentially transmitted Create, `unknown_delivery`
remains sticky unless `reconcile_create()` independently proves the unique
root-anchored thread. That proof replaces the in-process terminal outcome for
subsequent equal-key callers without enabling another POST or changing the
immutable request fingerprint. Adoption needs an actual verified root/thread,
not `CREATE_PUBLIC_THREADS`; the permission is checked immediately before a
new physical Create. Actual sends still require their own current ACL.


**Discord adapter integration Checkpoint 55 — cancellation-safe definite Create failure:**
A proved no-create outcome (`invalid_request`, `forbidden`, `not_found`,
`unsupported`, `rate_limited`, or a pre-POST `unavailable`) completes the
shared `CreateAttempt` Future and removes that exact flight with **no await**
between those operations. A shutdown cancellation therefore cannot strand
joined waiters behind a contended cleanup lock. The original flight's waiters
receive its definite error; a later separately admitted attempt can retry
only because the preceding physical write did not happen. A cancelled or
ambiguous physical POST continues to latch `unknown_delivery` and cannot be
replayed blindly. This coordinator remains process-local, not a durable fan-out
recovery ledger.
