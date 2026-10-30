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
**requires an externally supplied egress-vetted SDK factory and operation
handler**. This repository does **not yet provide a production factory** and
does not start `Client.fromDiscovery` with unrestricted REST/WebSocket egress.
`fromDiscovery()` alone cannot enforce the SSRF, DNS-rebinding and redirect
policy required for self-hosted instances. In the v3.3.0 source, REST uses a
shared `undici` Agent created inside `@fluxerjs/rest`, with no public per-client
request dispatcher configuration. Gateway does allow a WebSocket constructor.
A discovery-only preflight is therefore **not** a secure SDK factory. This
verified limitation requires an explicit reviewed egress containment solution
before the bot token can be exposed to the SDK.

`rest_reads.ts` now implements SDK-backed `fetch_channel`, `fetch_thread`
and `fetch_message` RPC reads with fresh channel lookups and strict physical
scope checks. It uses the SDK v3.3.0 `client.channels.fetch(id, {force:true})`
and `client.channels.fetchMessage(containerId, id)` public methods, and never
invents a forum starter-message ID. Media-bearing read snapshots explicitly
fail closed until their neutral contract is supported. This read handler is
injectable into the existing `FluxerNodeService`; production SDK client
creation and the remaining REST operations are still missing. The standalone
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
