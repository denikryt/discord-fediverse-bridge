# Discord-Lemmy Bridge

Python bridge that syncs content between Discord forum channels and Lemmy communities.

Subscriptions are managed dynamically via Discord slash commands — any number of forum channels can be bridged to Lemmy communities at runtime without restarting the bot.

## What is synced

- Discord forum thread → Lemmy post
- Discord message inside a forum thread → Lemmy comment
- Lemmy post → Discord forum thread + starter message (via Fedify HTTP intake)
- Lemmy comment → Discord message inside the mapped forum thread

Only create events are synced. Edits, deletes, attachments, and vote sync are not implemented.

## Requirements

- Python 3.12+
- A Discord bot with **message content intent** enabled and **applications.commands** scope
- A dedicated Lemmy user for the bridge
- A running Fedify gateway (for Lemmy → Discord direction)

## Configuration

Copy `.env.example` to `.env` and fill in:

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | yes | Discord bot token |
| `LEMMY_BASE_URL` | yes | Base URL of the Lemmy instance, e.g. `https://lemmy.ml` |
| `LEMMY_USERNAME_OR_EMAIL` | yes | Bridge bot Lemmy account |
| `LEMMY_PASSWORD` | yes | Bridge bot Lemmy password |
| `FEDIFY_SHARED_SECRET` | yes | Shared secret for internal HTTP communication with the Fedify gateway |
| `DISCORD_FORUM_CHANNEL_ID` | no | Legacy: channel ID of a single forum channel to subscribe on first startup |
| `LEMMY_COMMUNITY_NAME` | no | Legacy: short community name for the above channel, e.g. `general` |
| `LEMMY_COMMUNITY_ACTOR_ID` | no | Legacy: ActivityPub actor ID for the above community |
| `DATABASE_URL` | no | SQLite path or other SQLAlchemy URL, defaults to `sqlite:///./bridge.db` |
| `INTERNAL_HTTP_HOST` | no | Host for the internal HTTP server, defaults to `127.0.0.1` |
| `INTERNAL_HTTP_PORT` | no | Port for the internal HTTP server, defaults to `8080` |
| `BRIDGE_DISPLAY_PREFIX` | no | Prefix added to bridged messages, defaults to `[bridge]` |
| `LOG_LEVEL` | no | Logging level, defaults to `INFO` |

The three legacy variables (`DISCORD_FORUM_CHANNEL_ID`, `LEMMY_COMMUNITY_NAME`, `LEMMY_COMMUNITY_ACTOR_ID`) create a default subscription on first startup so existing deployments continue working. They can be removed from `.env` after the first run.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Run

```bash
python -m src.app
```

## Managing subscriptions

Subscriptions link a Discord forum channel to a Lemmy community. All commands require the **Manage Channels** permission.

| Command | Description |
|---|---|
| `/subscribe-channel` | Subscribe a forum channel to a Lemmy community. The community list is fetched from the configured Lemmy instance. |
| `/unsubscribe-channel` | Remove a channel's subscription. |
| `/list-subscriptions` | Show all active channel-community pairs. |

Each forum channel can have at most one subscription.

## Architecture

The bridge runs two concurrent processes in one Python instance:

- **Discord bot** — handles outbound sync (Discord → Lemmy) and slash commands
- **Internal HTTP server** — receives normalized events from the Fedify gateway for inbound sync (Lemmy → Discord)

Subscriptions and message mappings are persisted in SQLite (or any SQLAlchemy-compatible database).
