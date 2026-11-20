# Fluxer-only #12 — simplicity review follow-up

This change implements the two local simplifications recommended for commit
`4025fc2`, without changing the public transport contracts or the pre-migration
ownership policy. Production Node still verifies forum metadata and ACL at the
SDK boundary; Python does not cache RPC capability responses.

## SF01 — one forum capabilities read

`FluxerAdapter._preflight()` now uses one fresh `capabilities(channel_id)` RPC
response to validate the exact verified channel/guild, `forum_posts`, required
tag policy, selected tag inventory and moderated tags. Other callers retain the
public `capabilities()` method. The new stateful RPC regression checks exactly
one capability read and one Create effect for a successful forum publication.

## SF02 — one classifier per SQLite ownership signal

`preflight_instance_mode()` uses the existing direct connector-FK traversal
for `platform_channel_surfaces` and the shared `discord_*_id` projection scan
for both canonical `discord_guild_id` fields. The separately hard-coded surface
JOIN and two guild projection queries were removed. The previous surface-specific
error text remains available from the generic FK branch. Independent checks
for `source_platform='discord'`, legacy `source_*_id` canonical groups and
Discord-only tables remain unchanged.

## Verification and outstanding acceptance

- Before SF01, the new adapter test observed two capabilities RPCs and failed;
  after the change it passes with one RPC and one physical fake Create.
- `python -m unittest tests.behavior.test_fluxer_bot_create_adapter tests.behavior.test_fluxer_forum_create_node_crossprocess tests.behavior.test_fluxer_single_trusted_provisioning -q`:
  **61 tests passed**. SDK/HTTP boundaries were faked; SQLite used the real schema.
- New SQLite regression confirms that `discord_guild_id` without a Discord FK
  still refuses startup for both `message_mappings` and
  `published_activity_objects`, leaving the DB byte-identical; a valid bound
  Fluxer DB remains eligible afterward.
- The real Fluxer REST/Gateway and Docker Compose acceptance required by S19
  remains **not run**. This is a simplification only, not production acceptance.
