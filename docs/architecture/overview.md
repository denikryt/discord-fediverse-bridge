# Architecture overview

This document explains the top-level process boundary of the bridge, the major Python and gateway entry points, and the shortest reading path for a maintainer entering the codebase. It owns the broad architecture map only; detailed route, database, event-flow, and internal contract notes live in the focused architecture documents linked from here.

## Processes

```text
Discord API
  -> src/discord_bot.py
  -> src/discord_event_router.py
  -> src/community_sync/runtime.py or src/local_communities/runtime.py
  -> src/content_sync/*
  -> src/fedify_gateway_client.py
  -> fedify-gateway internal route
  -> signed ActivityPub delivery

Remote ActivityPub server
  -> fedify-gateway /inbox
  -> fedify-gateway/src/normalize.ts
  -> fedify-gateway/src/python-bridge.ts
  -> src/http_api.py /internal/activitypub/events
  -> src/activitypub_handlers.py
  -> runtime-specific handler
  -> Discord or relay fanout
```

physical surface mapping defines an **inactive** connector-scoped [Platform API](platform-api.md) (SDK-independent references, event shapes, lookup/delivery/permission ports, and a non-owning adapter registry). initial persistence implementation physical identities live in SQLite; Discord Gateway ingress and fanout **still execute the existing Discord-specific paths** until the Discord adapter and durable fan-out integration cutover. A configured Fluxer connector row does not start an adapter or Gateway client.

The Python bridge is the orchestration and persistence process. It starts the Discord bot, exposes registration and private gateway intake routes, owns SQLite state through SQLAlchemy, routes Discord events, and decides which runtime handles a bridge action.

The Fedify gateway is the ActivityPub/WebFinger edge. It owns public actor/object routes, WebFinger discovery, inbound ActivityPub normalization, signed outbound delivery, and the internal HTTP actions that Python calls when it needs federation work performed. Route handler bodies live under `fedify-gateway/src/routes/*`, while `server.ts` remains the composition and route-registration root.

## Main Python entry points

- `src/app.py` — composes settings, database, Discord bot, FastAPI routes, runtime services, named runtime binds, and process startup/partial-failure cleanup.
- `src/runtime.py` — stores long-lived services that handlers share after startup validation has completed.
- `src/runtime_ports.py` — declares narrow process-local ports used where startup wiring still has real lifecycle cycles.
- `src/http_api.py` — exposes public registration/OAuth routes and the private gateway-to-Python ActivityPub event route.
- `src/discord_bot.py` — connects Discord callbacks to bridge code and delegates raw mutation policy to a configured handler.
- `src/discord_event_router.py` — chooses remote subscription mode or local community mode for Discord events using the narrow mutation context supplied by startup wiring.
- `src/db/database.py` and `src/models.py` — persistence API and schema.

## Main gateway entry points

- `fedify-gateway/src/server.ts` — Hono application composition, trusted gateway dependency wiring, and route registration order.
- `fedify-gateway/src/routes/*` — public discovery/ActivityPub routes, internal command route bodies, health, response helpers, and inbox logging middleware.
- `fedify-gateway/src/federation.ts` — Fedify integration and inbound ActivityPub handling.
- `fedify-gateway/src/outbound/*` — signed transport and outbound use-case modules for follow publication, content publication, local-community fanout, personal-feed rendering, content mutations, relay delivery, and User Actor delivery batches.
- `fedify-gateway/src/federation-outbound.ts` — compatibility export layer for older gateway imports.
- `fedify-gateway/src/actor-store.ts`, `actors.ts`, and `webfinger.ts` — actor lookup, actor rendering, and discovery.
- `fedify-gateway/src/normalize.ts` and `python-bridge.ts` — inbound ActivityPub normalization and delivery to Python.


## Runtime composition and cleanup

`src/app.py` loads the migrated `discord-primary` connector from SQLite and refuses absent/incompatible metadata before the Gateway starts. Its registry binds exactly one live `DiscordAdapter` for that persisted identity. `Runtime.close()` attempts callback drain, both worker stops, SDK I/O quiesce, OAuth/Fedify close, DB close and deployment lease release in order even if an earlier cleanup step raises. A single failure is re-raised unchanged, while multiple failures are retained in ordered `ExceptionGroup`/`BaseExceptionGroup` rather than having a late release error replace the primary error. Each worker's `stop()` is responsible for joining its own task before shared resources close; runtime-level and SDK TaskGroup cancellation still require integration validation.

`src/app.py` is the only production composition root. It opens live owners in startup order, completes the remaining Discord/runtime cycles through named one-time bind methods, validates the graph before returning `Runtime`, and closes already-opened resources in reverse order if any later constructor, bind, or validation step fails. `Runtime.close()` remains the successful-startup shutdown owner. Raw Discord edit/delete paths receive `DiscordMutationContext` from `src/runtime_ports.py` rather than a broad `Runtime` or `BridgeBot` reference.

## Bridge modes

Remote community subscription mode binds a Discord forum channel to a remote Lemmy community. The shared bridge actor follows that community, inbound posts and comments fan out to subscribed Discord channels, and registered Discord users can publish outward.

Local community hosting mode exposes a Discord forum channel as a local ActivityPub Group actor. Remote actors can follow it, Discord content becomes local ActivityPub content, and inbound remote content can be mirrored or relayed according to local community runtime policy.

## Data ownership summary

Python owns the SQLite schema and all domain state. The gateway reads the same DB only where it needs actor documents, local community metadata, or published objects for ActivityPub serving. The gateway owns protocol delivery and signed ActivityPub HTTP interactions, not moderator-facing bridge policy.

## Where to start reading

Start with this document, then read `docs/architecture/bridge-modes.md` for conceptual boundaries, `docs/architecture/event-flows.md` for runtime traces, `docs/architecture/http-routes.md` for route ownership, `docs/architecture/database-map.md` for table ownership, and `docs/development/navigation.md` for task-oriented file pointers.

## What this document does not cover

This document does not define exact payload fields, every route, or every table invariant. Use the focused architecture documents for those details and the source code as the final implementation authority.

## Profile persistence and media ownership

Python owns profile persistence and bridge-managed profile image bytes. The main runtime holds a shared deployment-generation lease while DB and media paths are open; backup shares that generation, while restore and startup recovery require exclusive ownership. The Fedify gateway does not receive filesystem access. initial persistence implementation does not change actor serialization.

## User Actor profile delivery recovery

Profile mutations are owned by one synchronous `BEGIN IMMEDIATE` operation that writes the profile change, increments `users.profile_version`, stores immutable rendering input, snapshots accepted follower relationship incarnations, groups exact delivery endpoints, and records media ownership. Network rendering and transport occur only after commit.

`UserActorDeliveryService` renders and claims bounded work. Claims use durable `sending` ownership, stale-claim recovery, exact-incarnation eligibility, global federation policy, and a per-user/per-endpoint logical-version barrier. `UserActorDeliveryWorker` owns one process-lifetime asyncio task, drains immediately at startup, wakes after new profile sources, and is cancelled and awaited before the gateway client and database close.

## User Actor content mutation and follower fanout

Existing Discord-backed publication modes retain their authorization and routing ownership. After those checks, Create persistence atomically records the canonical gateway result, published-object mapping, immutable outbound source, and accepted-follower endpoint snapshot. Update and Delete first persist Discord-derived desired state and a stable mutation reservation; the gateway then validates the deterministic operation identity, serializes and signs the canonical activity, and returns the exact JSON that Python commits with the next object revision and follower source.

The existing `UserActorDeliveryWorker` is the single process-owned recovery loop for both pre-source mutation reservations and post-source endpoint deliveries. Database claims and leases provide multi-process exclusion and restart recovery; no SQL transaction is held across Discord fetches or gateway HTTP. Private signing keys remain confined to the gateway boundary.


## Personal-feed root publication

personal feed publication adds a dedicated `PersonalFeedPublisher` between Discord routing and the existing User Actor follower-delivery domain. The publisher reloads the active binding, validates exact owner identity and global ban state, derives deterministic object/activity IDs plus a Discord-derived publication timestamp, asks the gateway for render-only `Create(Note)` JSON, and commits mapping, canonical object, follower snapshot, source, and deliveries in one SQLite `BEGIN IMMEDIATE` transaction.

`message_mappings` and `published_activity_objects` carry an explicit `community` or `personal_feed` context. Database checks require personal-feed rows to retain guild, channel, and message identity while forbidding a fabricated community URL. Gateway rendering occurs before the transaction because it has no delivery or signing side effect; the delivery worker is woken only after a newly created source commits. Existing community publication, reply routing, mutation handling, and the empty User Actor outbox remain separate contracts.

### User Actor authored timeline

The gateway renders User Actor outboxes from a fixed-size internal DTO. Python owns eligibility, count, ordering, and the single read transaction; the gateway owns ActivityStreams rendering and canonical navigation links. Operational delivery metadata remains local CLI-only.

## Platform persistence boundary

Connector-scoped persistence separates domain actors from physical connector/channel locations. `src/db/repositories/platform_surfaces.py` owns placement resolution and typed claim mutations. Discord routing uses current `discord-primary` claims; connector-scoped historical copies survive release and rebinding. Fluxer rows are persistence-only at present. SQLite 3.31+ is required for stored generated roles; PostgreSQL 12+ DDL compatibility does not imply a PostgreSQL runtime migration implementation.

Database startup supports released v0.1.0 SQLite sources at version 0 and the complete current model at version 1. It upgrades directly in one transaction; The direct transfer work items the released rows in SQL TEMP, creates the current ORM target once and copies them natively; no legacy schema inventory business schema is reconstructed. See [SQLite startup migrations](../development/database-migrations.md).
