from __future__ import annotations

from html import unescape


DISCORD_MESSAGE_LIMIT = 2000
DISCORD_THREAD_NAME_LIMIT = 100


def truncate(text: str, limit: int) -> str:
    # Truncation keeps bridge-generated output valid for the destination
    # platform without silently blowing past hard limits.
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def normalize_text(text: str | None) -> str:
    # Normalize mixed HTML/plain-text input into a predictable string before
    # formatting cross-platform messages.
    if not text:
        return ""
    return unescape(text).strip()


def format_lemmy_post_for_discord(author: str, title: str, body: str, url: str) -> str:
    parts = [f"**{title.strip()}**", f"Author: `{author}`"]
    if body.strip():
        parts.append(body.strip())
    parts.append(url)
    return truncate("\n\n".join(parts), DISCORD_MESSAGE_LIMIT)


def format_lemmy_comment_for_discord(author: str, body: str, url: str) -> str:
    parts = [f"Comment from `{author}`"]
    if body.strip():
        parts.append(body.strip())
    parts.append(url)
    return truncate("\n\n".join(parts), DISCORD_MESSAGE_LIMIT)


def format_discord_body_for_lemmy(author: str, content: str, prefix: str) -> str:
    cleaned = normalize_text(content)
    header = f"{prefix} From Discord user **{author}**"
    if cleaned:
        return f"{header}\n\n{cleaned}"
    return header


def format_thread_title_for_discord(title: str) -> str:
    return truncate(normalize_text(title) or "Untitled Lemmy Post", DISCORD_THREAD_NAME_LIMIT)
