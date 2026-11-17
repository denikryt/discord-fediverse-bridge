# Fluxer Node connector — SDK integration in progress

The Node service owns one Fluxer SDK session, six public SDK event
subscriptions, a single SDK-object → neutral DTO converter, authenticated
Node → Python event forwarding, and a private Python → Node RPC listener.
Python still owns forum/physical SQLite claims and the issue #13 publication
gate; receiving a Gateway event is not an ActivityPub publication.

The public SDK v3.3.0 source exposes `ThreadCreate`, `ThreadUpdate`,
`ThreadDelete`, `MessageCreate`, `MessageUpdate`, `MessageDelete` and the
`ShardDisconnect`/`ShardResumed`/`ShardReady` lifecycle events. `ThreadCreate`
may represent a thread becoming visible rather than a newly created post.
Only the SDK's typed camelCase event objects are mapped by `normalization.ts`;
old raw dispatch-shape fixtures are not a production event path.

`FluxerNodeService` in `src/service.ts` owns the SDK session, event
forwarder, listeners, and private RPC listener. `src/main.ts` now provides
its one-instance **SDK-backed** production bootstrap: tokenless pinned-DNS
discovery, explicit operator-approved `api_public` and Gateway hosts, a
Node-only token file, the official SDK `Client` constructor, and an SDK
WebSocket constructor hook that rejects an unexpected Gateway host before
opening a socket. The factory supplies the complete discovered SDK endpoint
map; SDK v3.3.0 otherwise fills absent values with hosted defaults. SDK REST
uses its standard Undici Agent under the new *operator-trusted* threat model;
this **does not** provide a per-socket SSRF or DNS-rebinding defense.

Production `FLUXER_VERIFIED_WRITE_CONTRACT` defaults to `false`: effectful
RPC operations are rejected and write capability flags remain false before
real-server acceptance. With an explicitly verified test deployment, the
production capability response requires both fresh channel/tag facts and
current action-specific bot ACL. The executor independently checks the exact
message owner/permissions at write time; neither a capability response nor
an operator flag substitutes for verified live REST/Gateway readbacks.

The entrypoint additionally exposes a conservative `capabilities` RPC. For
forums, it reads the exact current channel through SDK `channels.fetch` and
then via SDK REST `get(Routes.channel(id))`; this second read avoids accepting
cached tags or `flags=0` synthesized when the server omitted these fields.
The response reports observed `requires_tag` (source constant `1 << 4`),
`available_tags` and each tag's `moderated` property. Bot-only forum Create
is advertised only with independently observed complete tag policy; thread
reply and message mutation methods require a live unarchived/unlocked thread
with a verified parent, or a supported text/announcement container. Every
actual write still requires a fresh bot ACL check and SDK owner/scope proof.
`create_reply_thread`, webhook identity and attachments are unsupported.
`SEND_MESSAGES_IN_THREADS` uses server bit 38, independently from ordinary
`SendMessages`. The current SDK-boundary tests are not live-instance proof.

`src/message_send.ts` additionally implements an **isolated SDK-backed message
sender** for exact text-channel messages and thread replies. It uses the
published SDK `channels.send(container, {content,replyTo,allowedMentions})`
API so the SDK itself serializes the `message_reference`. Every reply first
reads the exact immediate container and existing parent message, and refuses
stale/wrong parent or guild, archived/locked thread and unverified bot rights.
All sends suppress `parse` mentions and reply-author notifications; attachments
and non-plain content are deliberately rejected rather than dropped. The
received SDK `Message` must attest the same bot, exact container and
`messageReference`; guild scope comes from that freshly fetched container.
An explicit conflicting message guild is rejected, while SDK `guildId: null`
is accepted for the full REST response. Any mismatched receipt leaves the
existing CreateAttempt `unknown_delivery`.
The unversioned server source names `SEND_MESSAGES_IN_THREADS` as bit 38;
the executor and permissions RPC both check it. Production `main.ts` now
routes physical forum Create, nested replies, and bot-owned Edit/Delete via
the existing SDK executors, using one authenticated SDK client. A lost or
unverifiable write receipt is `unknown_delivery`, never an automatic retry.
Real Fluxer build/SDK permission and receipt verification is still required
before treating this as accepted for production.

A separate `src/forum_create.ts` now implements the **narrow confirmed SDK
REST forum-create wire contract** for testing: the SDK v3.3.0
`ThreadManager.createPost()` calls `client.rest.post(Routes.channelThreads(id))`
with the starter `message` in its request, but discards the response's
`message` when it returns a `ThreadChannel`. The supplied unversioned Fluxer
server `ThreadCreationService.createForumPost()` returns both the thread
payload and the starter message; the bridge uses the **same SDK REST client**
to preserve the two observed IDs. It validates the current forum metadata,
selected tags, bot identity, bot ViewChannel/SendMessages prerequisites,
request body and post-response scope, and reports `unknown_delivery` on
lost/malformed post responses. No second POST, guessed starter, custom HTTP
transport or webhook fallback is permitted. A two-process Python adapter →
authenticated Node RPC → stateful SDK REST fake tests this path.

**Release acceptance remains open.** The handlers are wired in the production
composition and covered by authenticated Node RPC / Python cross-process
SDK-edge tests. The earlier live REST trial on
`fluxer-release@2026.1106.212644` discovered incompatible receipt checks;
the corrected build has not been retested on a real Fluxer instance. Do not
interpret these tests as proof of a deployment-ready provider contract or
claim issue #12 is complete before that acceptance.

The Gateway `MessageCreate` event identifies its immediate container, not
necessarily its enclosing forum. `gateway.ts` now reuses the verified
`rest_reads.ts` thread lookup (forced SDK channel and parent reads) before
attaching `thread_context` / `parent_channel` to the single neutral DTO.
Contradicting guild, non-forum parent, missing SDK read and lookup timeout
result in an **unverified observation** with no claimable parent; the Python
SQLite owner alone decides forum membership and still never publishes AP in
issue #12. Each accepted lookup remains tracked and is joined on shutdown,
even if the event-forwarding deadline expires. The Node health diagnostic
`gateway_context_unverified` reports observed enrichment failures without
mistaking them for an outbound SDK readiness failure.

`rest_reads.ts` now implements SDK-backed `fetch_channel`, `fetch_thread`
and `fetch_message` RPC reads with fresh channel lookups and strict physical
scope checks. It uses the SDK v3.3.0 `client.channels.fetch(id, {force:true})`
and `client.channels.fetchMessage(containerId, id)` public methods, and never
invents a forum starter-message ID. Media-bearing read snapshots explicitly
fail closed until their neutral contract is supported. This read handler is
injectable into the existing `FluxerNodeService`.

`rest_reads.ts` includes the SDK's explicit `webhookId` on a complete
message read. Python `FluxerAdapter.fetch_message()` marks an author as
`is_bridge_owned` only when the Node RPC health currently reports READY with
an exact matching authenticated bot ID and the read explicitly reports
`webhook_id: null`. The current Node `health` must also attest the **exact configured instance
origin**; identical bot IDs from a different sidecar cannot grant mutation
authority. The first attested ID remains the expected connector
identity across sidecar restarts; a mismatch refuses mutation. Absent health,
foreign bot or webhook provenance never authorizes Edit/Delete. Tests use a
Node sidecar with a real SDK session lifecycle and private RPC and a stateful
SDK-edge fake, including a SQLite mutation CAS/receipt scenario. The physical SDK mutation methods
are exercised through the same RPC listener by `message_mutations.ts`;
real-server acceptance is still outstanding and production writes default off.

`permissions.ts` separately implements the `check_permissions` RPC operation
using `GuildMember.permissionsIn(channel)` and fresh SDK guild/member/role
reads. It checks forum-create `SEND_MESSAGES`, thread-send bit 38, and
read-history/view prerequisites for bot-owned edit/delete. A concrete message's
ownership remains the executor's responsibility; unsupported message-root
thread creation is still `unknown`. Missing or inconsistent roles, a foreign
bot or mismatched guild never grant rights. The standalone Node SDK factory
and entrypoint remain subject to published SDK type checks and live acceptance.

The npm dependency is pinned as `@fluxerjs/core@3.3.0` in `package.json`, but
the committed lockfile is checked in CI. This isolated review environment
cannot install the SDK, so TypeScript checking and real-instance Gateway
acceptance remain unverified here. `npm test` uses deterministic SDK-model-shaped
fixtures, local HTTP, and Node's type stripping. `npm run check` requires
installing dependencies and checking the actual published types. The source
contracts were independently inspected at the upstream `v3.3.0` Git tag;
that does not replace testing the published package.

Do not declare the Fluxer transport ready or enable remote bot writes until
that remaining SDK/factory/REST contract and the live acceptance tests pass.

## One trusted instance and credentials

Issue #12 accepts at most one explicitly approved Fluxer instance per bridge
runtime. Python's `FLUXER_CONNECTORS_JSON` contains the canonical origin,
connector key, `http://127.0.0.1:<fixed-port>` RPC address and private IPC
bearer only. A legacy `bot_token` property in that JSON is rejected; the bridge
never receives or forwards the Fluxer SDK bot token. Existing mismatched saved
connector identities are rejected without silent replacement.

The Node entrypoint in `src/main.ts` reads the credential from its **own
read-only secret file** using `readBotTokenFromFile()` in `token_secret.ts`.
Only Node may mount that file; it must not be placed in the broad bridge
`.env`/`env_file`, Python environment or DB. The optional Compose secret mount and Node-only image are wired in
`compose.yaml` / `fluxer-connector/Dockerfile`, but not yet built or tested
inside a real Docker Compose runtime. The Node process requires
`FLUXER_CONNECTOR_KEY`, `FLUXER_ORIGIN`, `FLUXER_BOT_TOKEN_FILE`,
`FLUXER_INTERNAL_BEARER`, `FLUXER_APPROVED_API_HOST`,
`FLUXER_APPROVED_GATEWAY_HOST`, `FLUXER_RPC_PORT`, and
`FLUXER_PYTHON_INGRESS_URL`. They must refer to the same persisted connector
identity and private shared-namespace loopback RPC/ingress endpoints. HTTP/WS
is restricted to explicitly opted-in loopback-only development. The shared
private HTTP bearer is a separate credential, not the SDK bot token.

## Fluxer bot message edits/deletes (local SDK-edge verification only)

`src/message_mutations.ts` uses the public SDK v3.3.0 `Message.edit()` and
`Message.delete()` methods after a **fresh SDK REST message read**, exact
container verification in its freshly observed guild, current authenticated bot-ID confirmation,
explicit `webhookId === null` and a fresh guild/channel ACL check. The
Python mutation coordinator still owns the SQLite CAS claim, revision and
unknown-effect fence. An edit ACK must preserve the original message ID,
container, nonconflicting guild, bot provenance and requested text; deletion never deletes
the surrounding thread. A lost SDK response or mismatched edit result is
`unknown_delivery`, not proof of a completed canonical revision. Definitive
SDK HTTP status codes (400/401/403/404/429) are classified separately.
The Node RPC tests and the two-process Python/Node/SQLite scenario use this
same executor with a stateful fake SDK edge. `main.ts` routes mutations only
when `FLUXER_VERIFIED_WRITE_CONTRACT=true`; otherwise direct production writes
and capability flags remain disabled until real-server acceptance.

## Compose packaging status

The opt-in `fluxer-sidecar` shares the Python `bridge` network namespace and
uses the fixed loopback RPC port 8769; no Node port is published. Compose
mounts `/run/secrets/fluxer-bot-token` in Node only. The release workflow
publishes a matching image only when an audited npm lockfile is committed.
The Dockerfile fails closed without that file. Docker, npm registry and a
real Fluxer instance were unavailable during these checks; the existing
SDK-fake tests cannot establish B20/B21 or authorize production writes.

## Official SDK write type contracts

`npm run check` includes `src/sdk_write_contracts.ts`: every isolated write factory must accept the pinned official `Client`. Message send and mutation handlers use SDK channel types and narrow guild/thread channels before reading guild-specific fields. Node tests also construct an actual SDK client and DM channel to verify rejection before message lookup or write. This does not enable production writes or replace real-server REST acceptance.

## Measured REST message scope and receipts

The independently tested `fluxer-release@2026.1106.212644` omits `guild_id` and
`webhook_id` on ordinary full REST messages. SDK 3.3.0 constructs `guildId: null`
and `webhookId: null`; message sends, replies, reads and mutations accept this
shape only after fetching the exact guild container. An explicit mismatched
guild or container still fails. SDK snapshots must retain their explicit
`webhookId` property, and only null can attest a bot-owned message.

Forum POST responses contain a thread with authoritative guild/parent and an
embedded starter with its own ID, exact channel, content and bot author. The
server's Message schema makes raw `webhook_id` optional/null; a non-null value
is never a bot receipt. A starter's observed ID may equal the thread ID or
differ from it. Both values must be present in the response; no ID is inferred.

`tests/message_rest_contracts.test.mjs` exercises these measured response shapes
through real SDK managers and Message instances with a stateful external REST
fake. Python/Node scenarios cover both ID relationships, repeated attempts,
lost responses and SQLite-owned mutations. These are local regressions, not a
live rerun of the corrected bridge. See
`docs/development/fluxer_rest_contract_fixes_ru.md` for results and open gates.

## Reproducible Node 22 build

Use Node 22.13 or newer in the Node 22 line: `npm run check:lock`, `npm ci --ignore-scripts`, `npm run check`, then `npm test`. The committed lock pins the official SDK and all transitive packages with integrity hashes. The Docker dependency stage runs these checks before pruning development dependencies. Release validation requires the lock and the Fluxer image; a missing lock or failing image cannot produce a successful release. Container builds are exercised in CI, while local two-container/live acceptance is a separate deployment check.
