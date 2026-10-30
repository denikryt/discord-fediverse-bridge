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

`FluxerNodeService` in `src/service.ts` composes the SDK session, event
forwarder, listeners, and private RPC listener under one shutdown owner. It
still **requires an externally supplied SDK factory and operation handler**;
this repository does not yet provide the production factory or activate bot
credentials. The revised one-trusted-instance issue #12 plan permits the
SDK's default Undici REST Agent after strict operator approval of endpoints.
It does **not** claim per-socket DNS-rebinding/SSRF protection. A production
factory must still enforce the revised trust/credential boundaries before
login and requires separate implementation and acceptance.

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

`permissions.ts` separately implements the `check_permissions` RPC operation
using `GuildMember.permissionsIn(channel)` and fresh SDK guild/member/role
reads. It confirms only view, read-history and send-message flags; forum
creation, thread creation and message-specific edit/delete stay `unknown`
until their separate API/ownership conditions are verified. Missing or
inconsistent roles, a foreign bot or mismatched guild never grant rights.
Production SDK client creation and the remaining REST operations are still
missing. The standalone
production process entry point also remains unimplemented.

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
