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
its one-instance **read-only** production bootstrap: tokenless pinned-DNS
discovery, explicit operator-approved `api_public` and Gateway hosts, a
Node-only token file, the official SDK `Client` constructor, and an SDK
WebSocket constructor hook that rejects an unexpected Gateway host before
opening a socket. The factory supplies the complete discovered SDK endpoint
map; SDK v3.3.0 otherwise fills absent values with hosted defaults. SDK REST
uses its standard Undici Agent under the new *operator-trusted* threat model;
this **does not** provide a per-socket SSRF or DNS-rebinding defense.

The entrypoint additionally exposes a conservative `capabilities` RPC. For
forums, it reads the exact current channel through SDK `channels.fetch` and
then via SDK REST `get(Routes.channel(id))`; this second read avoids accepting
cached tags or `flags=0` synthesized when the server omitted these fields.
The response reports observed `requires_tag` (source constant `1 << 4`),
`available_tags` and each tag's `moderated` property, but **all physical
write capabilities stay false** until the actual operator-pinned server build
has been measured. `tag_requirements_verified` only asserts the presence and
consistency of the current REST metadata; it is not a supported-create claim.
Malformed, missing or mismatched tag metadata cannot be used for POST.
The SDK's public `PermissionFlags` do not expose the server's separate
`SEND_MESSAGES_IN_THREADS` bit: thread sends remain `unknown` even with
ordinary SendMessages. A definitely absent prerequisite View/Send flag can
still produce `denied` for forum post admission.

The entrypoint exposes only confirmed SDK-backed physical reads and partial
ACL checks. Create, Send, Edit, Delete and reconciliation
are **not implemented** in the production handler and refuse RPC rather than
inventing server-specific REST methods. Do not enable production physical
writes or claim complete issue #12 transport at this checkpoint. The installed
SDK/typecheck and real Fluxer instance are not yet verified.

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
SDK-edge fake, including a SQLite mutation CAS/receipt scenario; live
Fluxer REST mutations are **not** claimed implemented by this fixture.

`permissions.ts` separately implements the `check_permissions` RPC operation
using `GuildMember.permissionsIn(channel)` and fresh SDK guild/member/role
reads. It confirms only view, read-history and send-message flags; forum
creation, thread creation and message-specific edit/delete stay `unknown`
until their separate API/ownership conditions are verified. Missing or
inconsistent roles, a foreign bot or mismatched guild never grant rights.
The standalone Node SDK factory and entrypoint are now implemented but require
installation of the actual SDK for TypeScript and live compatibility checks.
The remaining REST operations are still missing.

The npm dependency is pinned as `@fluxerjs/core@3.3.0` in `package.json`, but
npm registry DNS was unavailable during this implementation pass. There is
**no verified `package-lock.json`, installed SDK type check, or real-instance
Gateway acceptance result**. `npm test` uses deterministic SDK-model-shaped
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
`.env`/`env_file`, Python environment or DB. The optional Compose secret mount
and production container are **not yet wired**. The Node process requires
`FLUXER_CONNECTOR_KEY`, `FLUXER_ORIGIN`, `FLUXER_BOT_TOKEN_FILE`,
`FLUXER_INTERNAL_BEARER`, `FLUXER_APPROVED_API_HOST`,
`FLUXER_APPROVED_GATEWAY_HOST`, `FLUXER_RPC_PORT`, and
`FLUXER_PYTHON_INGRESS_URL`. They must refer to the same persisted connector
identity and private shared-namespace loopback RPC/ingress endpoints. HTTP/WS
is restricted to explicitly opted-in loopback-only development. The shared
private HTTP bearer is a separate credential, not the SDK bot token.
