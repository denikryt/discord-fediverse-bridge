# Discord-Lemmy Bridge Plan

## Goal

Build a simple Python bot which bridges content in both directions between:

- one Discord forum channel
- one Lemmy community

The first version should support:

- configuring a Lemmy base URL
- configuring a Discord forum channel ID
- forwarding new Discord threads and thread messages to Lemmy
- forwarding new Lemmy posts and comments to Discord
- basic loop prevention

This first version does not need:

- delete sync
- reaction / vote sync
- edit sync
- attachment sync
- rich formatting
- multi-community support

## Mapping Model

Lemmy has a post/comment structure, so the Discord side should use a Discord forum channel and forum threads.

Use this mapping:

- Discord forum channel = Lemmy community
- Lemmy post = new Discord forum thread + forum thread starter message
- Lemmy comment = Discord message inside the mapped forum thread
- Discord new forum thread = Lemmy post
- Discord message inside forum thread = Lemmy comment in the mapped post

This is the simplest version that still matches Lemmy reasonably well.

## Recommended Stack

- Python 3.12+
- `discord.py`
- `httpx`
- `SQLAlchemy`
- `alembic`
- `pydantic-settings`
- SQLite for the first version

SQLite is enough for a first deployment.

## Configuration

The bot should read configuration from environment variables.

Required variables:

- `DISCORD_TOKEN`
- `DISCORD_FORUM_CHANNEL_ID`
- `LEMMY_BASE_URL`
- `LEMMY_USERNAME_OR_EMAIL`
- `LEMMY_PASSWORD`
- `LEMMY_COMMUNITY_NAME`

Optional variables:

- `DATABASE_URL`
- `POLL_INTERVAL_SECONDS`
- `BRIDGE_DISPLAY_PREFIX`
- `LOG_LEVEL`

Example:

```env
DISCORD_TOKEN=...
DISCORD_FORUM_CHANNEL_ID=123456789012345678
LEMMY_BASE_URL=https://forum.nu31.space
LEMMY_USERNAME_OR_EMAIL=discord_bridge
LEMMY_PASSWORD=...
LEMMY_COMMUNITY_NAME=general
DATABASE_URL=sqlite:///./bridge.db
POLL_INTERVAL_SECONDS=5
BRIDGE_DISPLAY_PREFIX=[bridge]
LOG_LEVEL=INFO
```

## Functional Behavior

### Discord -> Lemmy

When a new forum thread appears in the configured Discord forum channel:

- ignore bot-created forum threads
- ignore forum threads created by the bridge bot itself
- use the Discord forum thread title as the Lemmy post title
- use the first forum thread message as the Lemmy post body
- create a new Lemmy post in the configured community
- store the forum-thread/post mapping in the database

Suggested body format:

```markdown
From Discord user **<author name>**

<thread starter message>
```

When a new message appears inside a mapped Discord forum thread:

- ignore bot messages
- ignore messages created by the bridge bot itself
- find the mapped `lemmy_post_id`
- create a new Lemmy comment for that post
- store the message/comment mapping in the database

Suggested body format:

```markdown
From Discord user **<author name>**

<message content>
```

### Lemmy -> Discord

The bot should poll Lemmy every few seconds.

It should fetch:

- new posts in the configured community
- new comments in the configured community

For each unseen post:

- ignore content created by the Lemmy bridge account
- create a new forum thread in the configured Discord forum channel
- use the Lemmy post title as the Discord forum thread title
- send the Lemmy post body as the first forum thread message
- store the forum-thread/post mapping in the database

For each unseen comment:

- ignore content created by the Lemmy bridge account
- find the mapped Discord forum thread for the related Lemmy post
- send a message into that forum thread
- include author, comment text, and a link to the comment
- store the message/comment mapping in the database

## Loop Prevention

This is mandatory even in version 1.

The bridge must avoid re-importing content it created itself.

Use both of these protections:

1. Ignore messages and forum threads from the Discord bot user.
2. Ignore Lemmy posts and comments created by the dedicated bridge account.

Also store mappings and source IDs in the database.

## Database Schema

Use a small schema.

### `post_links`

- `id`
- `lemmy_post_id`
- `discord_forum_thread_id`
- `discord_starter_message_id`
- `direction`
- `created_at`

### `comment_links`

- `id`
- `lemmy_comment_id`
- `lemmy_post_id`
- `discord_forum_thread_id`
- `discord_message_id`
- `direction`
- `created_at`

### `sync_state`

- `id`
- `key`
- `value`
- `updated_at`

The poller should store high-water marks such as:

- last seen Lemmy post timestamp
- last seen Lemmy comment timestamp

## Proposed Project Structure

```text
discord-lemmy-bridge/
  PLAN.md
  README.md
  pyproject.toml
  .env.example
  src/
    app.py
    config.py
    logging_setup.py
    db.py
    models.py
    discord_bot.py
    lemmy_client.py
    bridge_discord_to_lemmy.py
    bridge_lemmy_to_discord.py
    formatting.py
    poller.py
```

## Implementation Steps

### Step 1. Bootstrap Project

- create Python project
- add dependencies
- add `.env.example`
- add config loader
- add logging

### Step 2. Build Lemmy Client

Implement:

- login
- bearer token storage
- create post
- create comment
- list posts by community
- list comments by community

Use Lemmy API v4.

### Step 3. Build Discord Bot

Implement:

- login with bot token
- subscribe to forum-thread create events
- subscribe to message create events
- filter by configured forum channel ID and mapped forum-thread IDs
- create forum threads in the configured forum channel
- send plain messages into forum threads

The Discord app will need message content intent enabled.

### Step 4. Add Database Layer

Implement:

- SQLAlchemy models
- simple migration setup
- helper functions for mapping lookups
- helper functions for sync checkpoint storage

### Step 5. Implement Discord -> Lemmy

Behavior:

- receive new Discord forum thread
- create Lemmy post
- store forum-thread/post mapping
- receive new message inside mapped forum thread
- create Lemmy comment
- store message/comment mapping
- log success / failure

### Step 6. Implement Lemmy Poller

Behavior:

- poll new posts
- poll new comments
- compare against stored state and mappings
- create unseen forum threads in Discord for new Lemmy posts
- send unseen comments into the correct Discord forum thread
- update stored checkpoints

### Step 7. Add Minimal Operational Safety

- retries for transient HTTP failures
- rate-limit friendly polling
- structured logs
- startup validation for config

## First Version Limitations

The first version will still be intentionally limited:

- Discord reply-to-message will not be mapped precisely to Lemmy parent comments
- Lemmy nested comments will be flattened into normal forum-thread messages
- attachments are ignored
- edits are ignored

That is acceptable for version 1 if the goal is to prove the bridge flow.

## Suggested Version 2

After the first version works, the next upgrade should be:

- map Discord reply-to-message onto Lemmy parent comments
- add attachment forwarding
- add edit sync
- add multi-community support

## Success Criteria For Version 1

Version 1 is successful if:

- the bot starts from env config
- it connects to Discord
- it logs into Lemmy
- new Discord forum threads create Lemmy posts
- new Discord forum-thread messages create Lemmy comments
- new Lemmy posts create Discord forum threads
- new Lemmy comments appear in the correct Discord forum thread
- the bridge does not loop on its own content
- mappings persist across restarts
