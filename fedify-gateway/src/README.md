# fedify-gateway/src

## Purpose

This package is the ActivityPub/WebFinger edge for the bridge.

## Responsibility

Serve actor/object routes, normalize inbound ActivityPub, expose internal gateway actions, and sign outbound federation delivery.

## Not responsible for

Discord bot behavior, registration pages, moderator policy, or Python-owned persistence decisions.

## Primary entry points

`server.ts` registers routes, `federation.ts` handles inbound AP, `federation-outbound.ts` sends signed activities, `normalize.ts` builds Python events, `python-bridge.ts` delivers events to Python, `actor-store.ts` loads actors, `actors.ts` renders actors, `webfinger.ts` handles discovery, and `db.ts` reads bridge DB state.

## Important tables or payloads

`users`, `local_communities`, `published_activity_objects`, `remote_subscribers`, gateway internal route payloads, and normalized Python event types.
