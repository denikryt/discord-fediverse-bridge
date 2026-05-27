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

The Python bridge is the orchestration and persistence process. It starts the Discord bot, exposes registration and private gateway intake routes, owns SQLite state through SQLAlchemy, routes Discord events, and decides which runtime handles a bridge action.

The Fedify gateway is the ActivityPub/WebFinger edge. It owns public actor/object routes, WebFinger discovery, inbound ActivityPub normalization, signed outbound delivery, and the internal HTTP actions that Python calls when it needs federation work performed.

## Main Python entry points

- `src/app.py` — composes settings, database, Discord bot, FastAPI routes, runtime services, and process startup.
- `src/runtime.py` — stores long-lived services that handlers share.
- `src/http_api.py` — exposes public registration/OAuth routes and the private gateway-to-Python ActivityPub event route.
- `src/discord_bot.py` — connects Discord callbacks to bridge code.
- `src/discord_event_router.py` — chooses remote subscription mode or local community mode for Discord events.
- `src/db/database.py` and `src/models.py` — persistence API and schema.

## Main gateway entry points

- `fedify-gateway/src/server.ts` — Hono application and route registration.
- `fedify-gateway/src/federation.ts` — Fedify integration and inbound ActivityPub handling.
- `fedify-gateway/src/federation-outbound.ts` — signed outbound ActivityPub delivery.
- `fedify-gateway/src/actor-store.ts`, `actors.ts`, and `webfinger.ts` — actor lookup, actor rendering, and discovery.
- `fedify-gateway/src/normalize.ts` and `python-bridge.ts` — inbound ActivityPub normalization and delivery to Python.

## Bridge modes

Remote community subscription mode binds a Discord forum channel to a remote Lemmy community. The shared bridge actor follows that community, inbound posts and comments fan out to subscribed Discord channels, and registered Discord users can publish outward.

Local community hosting mode exposes a Discord forum channel as a local ActivityPub Group actor. Remote actors can follow it, Discord content becomes local ActivityPub content, and inbound remote content can be mirrored or relayed according to local community runtime policy.

## Data ownership summary

Python owns the SQLite schema and all domain state. The gateway reads the same DB only where it needs actor documents, local community metadata, or published objects for ActivityPub serving. The gateway owns protocol delivery and signed ActivityPub HTTP interactions, not moderator-facing bridge policy.

## Where to start reading

Start with this document, then read `docs/architecture/bridge-modes.md` for conceptual boundaries, `docs/architecture/event-flows.md` for runtime traces, `docs/architecture/http-routes.md` for route ownership, `docs/architecture/database-map.md` for table ownership, and `docs/development/navigation.md` for task-oriented file pointers.

## What this document does not cover

This document does not define exact payload fields, every route, or every table invariant. Use the focused architecture documents for those details and the source code as the final implementation authority.
