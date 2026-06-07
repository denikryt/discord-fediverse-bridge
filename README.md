# Discord-Lemmy Bridge

Python bridge plus a separate Fedify gateway that sync Discord forum threads with federated community actors such as Lemmy communities and compatible Mastodon-facing community flows.

## What This Service Enables

This service bridges Discord forum discussions with fediverse community-style
discussion spaces.

It can:

- connect a Discord forum channel to a remote community actor such as a Lemmy community
- expose a Discord forum channel as a local federated community actor
- publish Discord posts and comments into the fediverse
- mirror fediverse posts and comments back into Discord
- keep multiple subscribed Discord forum channels in sync for the same remote community
- keep one local community in sync across its host forum, local subscriber forums, and remote ActivityPub subscribers

The local community mode is designed to imitate the behavior of a Lemmy
community actor. That is the primary compatibility target. The same community
federation model also works with Mastodon in the supported flows, so the local
community appears there as a community-style actor rather than as an ordinary
single-user account.

## How It Works

The bridge has two main modes.

### 1. Remote community subscription mode

The bridge runs one shared **bridge actor** that follows remote communities on
behalf of Discord subscribers. When a moderator runs `/subscribe-channel`, the
bridge actor sends one shared `Follow` to the target community. Inbound posts
and comments are then fanned out to every subscribed Discord forum channel.

When the last subscribed Discord channel leaves that remote community, the
bridge sends `Undo(Follow)`.

### 2. Local community hosting mode

The bridge can expose a Discord forum as one local ActivityPub `Group` actor.
Remote actors can follow it, and Discord activity in that forum is published as
community content. Host forum messages, local subscriber forum messages, and
remote inbound ActivityPub content can all be synchronized through that local
community.

For outbound authorship, registered Discord users publish through their own
local ActivityPub user identities, so posts and comments stay attributed to the
correct person.

To send messages from Discord into the fediverse, a user must register on the
bridge. Use `/register` and complete the Discord login flow in the browser.

To expose a Discord forum as a local federated community, an allowlisted bot
operator can run `/create_community` with:

- `slug`
- `name`
- `description`
- the target Discord forum channel

Local community discovery is exposed through:

- `acct:!slug@your-gateway-host` for Discord-backed community actors
- `acct:username@your-gateway-host` for registered local Discord users
- `GET /.well-known/discord-fediverse-bridge/communities` for bridge-specific public community discovery used by `/subscribe-channel`

User handles are only local bridge identities for authorship and display in
the fediverse. They are not a remote follow target.

## Access control and federation policy

The bridge supports simple operator-side restrictions:

- you can explicitly list Discord user ids that are allowed to create local communities and subscribe channels to remote communities
- you can explicitly restrict federation to a specific allowlist of remote instance hostnames

These limits are configured through environment variables, so the bridge can be
run either in an open mode or in a tightly controlled mode.

## What Is Synced

Remote community mode:
- Discord forum thread starter → ActivityPub post
- Discord thread message → ActivityPub comment
- inbound federated post → Discord thread
- inbound federated comment → Discord message
- edit / delete propagate in both directions for supported flows

Local community mode:
- host Discord forum thread/message → local federated community content
- local subscriber forum thread/message → same local federated community content
- remote follower post/comment → mirrored into the local community's Discord surfaces
- local community create, edit, and delete fan out to other Discord surfaces and remote ActivityPub subscribers

Vote sync is not implemented.

## Processes

The project runs as two processes:

1. `Python bridge`
   - Discord bot
   - internal FastAPI server
   - registration backend
   - subscription state and message mappings in SQLite

2. `fedify-gateway`
   - ActivityPub protocol edge
   - local actor documents and WebFinger
   - outbound `Follow`, `Undo(Follow)`, `Create`, `Update`, `Delete`, and local-community relay delivery
   - inbound federation intake forwarded to Python


## Architecture and navigation

- [docs/architecture/overview.md](docs/architecture/overview.md) — process boundaries and major entry points
- [docs/architecture/bridge-modes.md](docs/architecture/bridge-modes.md) — remote subscriptions vs local communities
- [docs/architecture/event-flows.md](docs/architecture/event-flows.md) — step-by-step behavior traces
- [docs/architecture/http-routes.md](docs/architecture/http-routes.md) — Python/gateway route ownership
- [docs/architecture/database-map.md](docs/architecture/database-map.md) — table ownership and invariants
- [docs/architecture/gateway-python-contract.md](docs/architecture/gateway-python-contract.md) — internal API contract
- [FEDERATION.md](FEDERATION.md) — supported federation profile and protocol scope
- [docs/development/navigation.md](docs/development/navigation.md) — task-oriented reading guide

## Public dashboard

The Python bridge exposes a public dashboard on the root URL `/`. Legacy `/dashboard` links redirect to `/`. The JSON backing endpoint is `/dashboard/data`, and the dashboard web assets live under `/dashboard/static/`.

The dashboard is intentionally public and omits Discord guild/channel IDs, private keys, shared secrets, database paths, and internal service URLs. It does show last-known Discord guild and forum-channel names for hosted local communities, accepted remote subscriptions, and active same-instance local subscribers so operators can see where public routing state lives without exposing raw Discord identifiers.

## Discord bot commands

- `/register`
- `/subscribe-channel`
- `/unsubscribe-channel`
- `/list-subscriptions`
- `/create_community`

## Known issues

See `notes/known_issues.md` for the current short issue journal and verified behavior notes.

Deployment and runtime setup live in [`docs/DEPLOY.md`](docs/DEPLOY.md).
