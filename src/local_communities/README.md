# local_communities

## Purpose

This package implements Discord forum channels exposed as local ActivityPub Group actors, including remote-subscriber state and relay behavior.

## Responsibility

Local community runtime behavior, remote-subscriber follow acceptance, local content mappings, and relay fanout.

## Not responsible for

Remote Lemmy subscription binding, gateway WebFinger routing, or generic content helpers.

## Primary entry points

`service.py`, `runtime.py`, `federation_fanout.py`, `activitypub_renderers.py`, `delivery_mapping.py`, `inbound_mapping.py`, `reply_mapping.py`.

## Important tables or payloads

`local_communities`, `remote_subscribers`, local community thread/message tables, relay tables, `local_subscribers` control-plane rows, and `local.follow_requested` events.
