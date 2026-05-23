# community_sync

## Purpose

This package implements remote Lemmy community subscription synchronization, not local community hosting.

## Responsibility

Remote subscribed-channel runtime behavior, inbound Discord fanout, and remote subscription mapping.

## Not responsible for

Local community actor hosting, local follower state, or gateway route ownership.

## Primary entry points

`runtime.py`, `discord_fanout.py`, `delivery_mapping.py`, `inbound_mapping.py`, `reply_mapping.py`, `edit_delete.py`.

## Important tables or payloads

`channel_community_subscriptions`, `bridge_actor_follows`, community group tables, and normalized post/comment events.
