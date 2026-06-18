# fedify-gateway/src

## Purpose

This package is the ActivityPub/WebFinger edge for the bridge.

## Responsibility

Serve actor/object routes, normalize inbound ActivityPub, expose internal gateway actions, and sign outbound federation delivery.

## Not responsible for

Discord bot behavior, registration pages, moderator policy, or Python-owned persistence decisions.

## Primary entry points

`server.ts` registers routes, `federation.ts` handles inbound AP, `outbound/*` contains signed delivery and outbound use-case modules, `federation-outbound.ts` remains the compatibility export layer for those modules, `normalize.ts` builds Python events, `python-bridge-client.ts` provides the authenticated Python bridge client, `actor-store.ts` resolves actors through that client, `actors.ts` renders actors, and `webfinger.ts` handles discovery.

## Important tables or payloads

Authenticated `/internal/fedify/*` read-model payloads and normalized Python event types. The gateway does not own or read the database schema.
