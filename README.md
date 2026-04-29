# Discord-Lemmy Bridge

Simple Python bridge between:

- one Discord forum channel
- one Lemmy community

Current scope:

- Discord forum thread -> Lemmy post
- Discord message inside forum thread -> Lemmy comment
- Lemmy post -> Discord forum thread + starter message
- Lemmy comment -> Discord message inside the mapped forum thread

This is an MVP, not a full sync engine.

## Requirements

- Python 3.12+
- a Discord bot with message content intent enabled
- a dedicated Lemmy user for the bridge

## Configuration

Copy `.env.example` to `.env` and fill in:

- `DISCORD_TOKEN`
- `DISCORD_FORUM_CHANNEL_ID`
- `LEMMY_BASE_URL`
- `LEMMY_USERNAME_OR_EMAIL`
- `LEMMY_PASSWORD`
- `LEMMY_COMMUNITY_NAME`

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

## Notes

- The bridge stores mappings and polling checkpoints in SQLite by default.
- `LEMMY_COMMUNITY_NAME` should be the short community name, for example `general`.
- Only create events are synced.
- Attachments, edits, deletes, and precise parent-comment mapping are not implemented yet.
