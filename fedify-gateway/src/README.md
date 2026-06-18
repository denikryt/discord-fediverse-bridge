# fedify-gateway/src

## Purpose

This package is the ActivityPub/WebFinger edge for the bridge.

## Responsibility

Serve actor/object routes, normalize inbound ActivityPub, expose internal gateway actions, and sign outbound federation delivery.

## Not responsible for

Discord bot behavior, registration pages, moderator policy, or Python-owned persistence decisions.

## Primary entry points

`server.ts` composes the Hono app and mounts `routes/*`, `routes/public-activitypub.ts` and `routes/public-discovery.ts` own public route bodies, `routes/internal-*.ts` own Python-called command route bodies, `routes/inbox-logging.ts` owns pre-Fedify inbox logging and raw request capture, `federation.ts` handles inbound AP, `outbound/*` contains signed delivery and outbound use-case modules, `federation-outbound.ts` remains the compatibility export layer for those modules, `normalize.ts` builds Python events, `python-bridge-client.ts` provides the authenticated Python bridge client, `actor-store.ts` resolves actors through that client, `actors.ts` renders actors, and `webfinger.ts` handles discovery.

## Important tables or payloads

Authenticated `/internal/fedify/*` read-model payloads and normalized Python event types. The gateway does not own or read the database schema.
