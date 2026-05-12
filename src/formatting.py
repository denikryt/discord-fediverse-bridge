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


def format_lemmy_comment_for_discord(author: str, body: str, url: str, actor_id: str = "") -> str:
    # Show full fediverse handle when we can derive the instance domain from actor_id.
    # Falls back to plain username when actor_id is absent (e.g. synthetic events).
    if actor_id:
        try:
            from urllib.parse import urlparse
            domain = urlparse(actor_id).hostname or ""
            display = f"`{author}@{domain}`" if domain else f"`{author}`"
        except Exception:
            display = f"`{author}`"
    else:
        display = f"`{author}`"
    parts = [display]
    if body.strip():
        parts.append(body.strip())
    return truncate("\n\n".join(parts), DISCORD_MESSAGE_LIMIT)


def apply_edit_to_discord_message(
    current_content: str,
    new_body: str,
    fallback_header: str = "",
) -> str:
    """Replace the body of a bridge-formatted Discord message while keeping the header line.

    Bridge messages have the form:
        `username`

        body text

    When an edit arrives (from Discord or inbound AP), only the body changes.
    The header line is preserved verbatim so the author attribution is not lost.

    If the current content has no recognisable header and fallback_header is
    provided, a fresh backtick-quoted header is built from it. This covers
    older mirror messages created before the header format was introduced.
    If neither a header nor a fallback is available, the new body is returned as-is.
    """
    # The header is always the first paragraph (everything before the first \n\n).
    # Split on the first double-newline only so multi-paragraph bodies are kept intact.
    parts = current_content.split("\n\n", 1)
    header = parts[0].strip()
    new_body = new_body.strip()

    # Use the existing header when it looks like a bridge attribution (backtick-quoted).
    if header.startswith("`") and header.endswith("`"):
        if new_body:
            return truncate(f"{header}\n\n{new_body}", DISCORD_MESSAGE_LIMIT)
        return header

    # No recognisable header — build one from fallback_header when available.
    if fallback_header:
        header = f"`{fallback_header}`"
        if new_body:
            return truncate(f"{header}\n\n{new_body}", DISCORD_MESSAGE_LIMIT)
        return header

    # No header and no fallback — return the new body unchanged.
    return truncate(new_body, DISCORD_MESSAGE_LIMIT)


def format_discord_body_for_lemmy(author: str, content: str, prefix: str) -> str:
    # Phase 9: Remove the "[bridge] From Discord user" header — just return the content.
    cleaned = normalize_text(content)
    return cleaned


def format_thread_title_for_discord(title: str) -> str:
    return truncate(normalize_text(title) or "Untitled Lemmy Post", DISCORD_THREAD_NAME_LIMIT)
