# content_sync

## Purpose

This package contains reusable content publishing, reference, reply, persistence, edit, and delete helpers. It does not decide whether an event belongs to remote subscription mode or local community mode.

## Responsibility

Build outbound publish/update/delete requests, resolve reply references, and persist shared object/mapping state.

## Not responsible for

Runtime selection, Discord delivery, gateway routes, or ActivityPub signing.

## Primary entry points

`outbound_publish.py`, `inbound_references.py`, `reply_mapping.py`, `edit_delete.py`, `persistence.py`.

## Important tables or payloads

`message_mappings`, `published_activity_objects`, community group tables, and gateway publish/update/delete payloads.

## local subscriber synchronization dependency audit

Legacy `inbound_references.build_message_reference()` defers loading
`discord.py` until a legacy physical send actually requests an SDK reference.
Importing reusable content helpers and CommunityRuntime no longer loads the
Discord SDK. The remaining legacy SDK-based reference/edit/delete operations
must be removed after their callers are migrated; they are not Platform API.
