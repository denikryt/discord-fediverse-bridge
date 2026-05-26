# Event flows

This document explains the major runtime traces through the bridge: user/platform action, important preconditions, modules called, database effects, and outbound effects. It owns step-by-step behavior flow descriptions, not exhaustive function internals.

## Registration flow

1. `/register` starts in `src/commands/register.py` and browser routes in `src/http_api.py`.
2. Discord OAuth state is stored through `src/registration_service.py`.
3. Successful completion creates a `users` row with stable local actor URLs and key material.

## Remote /subscribe-channel flow

1. `src/commands/subscribe.py` calls `src/operations/subscribe.py`.
2. `src/lemmy_client.py` resolves the remote community when needed.
3. `src/db.py` records `channel_community_subscriptions` and bridge follow state.
4. `src/fedify_gateway_client.py` calls gateway `/follow-community`; gateway sends the Follow.
5. Accept returns through gateway `/inbox`, `src/http_api.py`, and `src/activitypub_handlers.py`.

## Remote /unsubscribe-channel flow

1. `src/commands/unsubscribe.py` calls `src/operations/unsubscribe.py`.
2. The channel subscription is removed.
3. If it was the last channel for the community, Python calls gateway `/unfollow-community`.
4. The bridge follow row is removed or preserved according to Undo(Follow) delivery outcome.

## Discord thread -> remote Lemmy community publish flow

1. `src/discord_bot.py` receives a Discord event.
2. `src/discord_event_router.py` selects remote subscription runtime.
3. `src/community_sync/runtime.py` validates context.
4. `src/content_sync/outbound_publish.py` builds the publish request.
5. Gateway `/publish` signs and sends Create; Python persists object/mapping state.

## Remote ActivityPub post -> Discord fanout flow

1. Gateway `/inbox` receives and normalizes ActivityPub to `post.created`.
2. `src/activitypub_handlers.py` validates idempotency and routes to remote subscription runtime.
3. If no accepted subscription and no existing mapped thread context exists, Python skips the event locally while gateway delivery remains acknowledged.
4. Otherwise Discord threads are created and group/delivery rows are recorded.

## Remote ActivityPub comment -> Discord fanout flow

1. Gateway normalizes inbound comment ActivityPub to `comment.created`.
2. Python validates the event and checks accepted subscription or already mapped parent/post context.
3. Unsubscribed comments without mapped context are skipped before backfill or Discord fanout.
4. Runtime creates Discord messages in mapped thread deliveries and records message group rows.

## Discord edit/delete -> ActivityPub update/delete flow

1. Discord edit/delete enters `src/discord_bot.py` and routes through `src/discord_event_router.py`.
2. Runtime resolves persisted ActivityPub object ids through `src/db.py`.
3. `src/content_sync/edit_delete.py` prepares the request.
4. Gateway `/update` or `/delete` signs and sends the ActivityPub operation.

## /create_community flow

1. `src/commands/create_community.py` calls `src/operations/create_community.py`.
2. `src/local_communities/service.py` creates stable local community actor metadata.
3. Python stores `local_communities`; gateway later serves the actor from DB.

## Discord thread -> local ActivityPub community publish flow

1. Router selects `src/local_communities/runtime.py` for the host forum or an active local subscriber forum of a local community.
2. Python records one canonical local-community thread/message row and a source Discord surface. Host sources create a `role="host"` source surface; local subscriber sources create a `role="local_subscriber"` source surface.
3. Python calls gateway `/publish-local-community`; gateway creates local ActivityPub content and delivers it to accepted remote subscribers.
4. Local Discord fanout creates missing target surfaces: host sources copy to local subscribers, while local subscriber sources copy to the host forum and sibling local subscribers.

## Remote ActivityPub Follow -> local community follower flow

1. Gateway receives Follow to a local community actor and emits `local.follow_requested`.
2. Python stores `remote_subscribers` and asks gateway to send Accept(Follow).

## Accept local community follow flow

1. Local runtime accepts a remote follow.
2. Python calls gateway `/accept-local-community-follow`.
3. Gateway signs Accept(Follow) as the local community actor.

## Remote ActivityPub content -> local community Discord/federation relay flow

1. Gateway normalizes content addressed to a local community.
2. Python routes by `community_actor_id` to local community runtime.
3. Runtime mirrors into the host Discord forum and records canonical rows plus host surfaces.
4. Local Discord fanout creates missing local subscriber surfaces for active same-instance subscriber forums.
5. Gateway `/send-local-community-relay` signs and delivers rendered relay activities to other accepted remote subscribers.

## Local community edit/delete flow

1. Discord edit/delete enters through `src/discord_event_router.py` and resolves the starter/reply surface row for the raw message id.
2. `src/local_communities/runtime.py` resolves the canonical post/comment and loads the `PublishedActivityObject` by canonical AP object id, not by the copied Discord message id.
3. For host or active local-subscriber source surfaces, Python sends one gateway Update/Delete and `src/local_communities/discord_fanout.py` mutates every other persisted local Discord surface.
4. For inbound remote update/delete, the runtime mutates every persisted local Discord surface, including host and local subscribers, then keeps the existing remote relay eligibility rule for remote subscribers.
5. Inactive local-subscriber surfaces are historical copies, not authoritative mutation sources.

## Remote ActivityPub Undo(Follow) -> local community unfollow flow

1. Gateway receives embedded Undo(Follow) addressed to a local community actor.
2. Gateway emits `local.unfollow_requested` when the target actor is owned by this bridge.
3. Python removes the remote actor from `remote_subscribers`.
4. Future create, update, and delete fanout uses current accepted followers and excludes the removed actor.


## Local subscriber Discord post/comment -> local community flow

1. `src/discord_event_router.py` resolves the source forum through `src/local_communities/participant_routing.py`.
2. Active `LocalSubscriber` forums route into `LocalCommunityRuntime`; inactive subscribers and unrelated forums do not.
3. Runtime publishes the source Discord starter/reply through the existing local-community publish path and persists one canonical row.
4. Runtime records the source `role="local_subscriber"` surface and then fans out to the host forum plus sibling active local subscribers.
5. Stage 5 handles later edits/deletes from the source local-subscriber surface through the participant-wide mutation flow above.
