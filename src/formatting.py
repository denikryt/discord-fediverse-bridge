"""Formatting helpers for Discord-facing bridge messages."""

from __future__ import annotations

from html import unescape
import re
from urllib.parse import urlparse, urlsplit


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


def format_fediverse_author_for_discord(author: str, actor_id: str = "") -> str:
    """Render a Discord-safe fediverse author label from an AP actor id.

    Comments already showed full remote handles when actor_id was available.
    Posts use the same helper so inbound post and comment attribution stay
    consistent while synthetic events without actor ids keep the old username
    fallback.
    """
    if actor_id:
        try:
            domain = urlparse(actor_id).hostname or ""
        except Exception:
            domain = ""
        if domain:
            return f"`{author}@{domain}`"
    return f"`{author}`"



# The feed-only layout protects complete links while retaining the unchanged
# Commons/NodeBB renderer and its historical truncate() behavior.
_FEED_LINK = re.compile(
    r"\[[^\]\n]+\]\((?:<https?://[^\s<>]+>|https?://[^\s)]+)\)"
    r"|<https?://[^\s<>]+>"
    r"|https?://[^\s<>]+"
)


def validate_feed_object_url(object_id: str) -> str:
    """Validate an immutable AP identity before embedding it in Discord text."""
    if not isinstance(object_id, str) or not object_id or any(
        ord(ch) < 32 or ord(ch) == 127 or ch in '<>\\`' for ch in object_id
    ):
        raise ValueError('invalid canonical feed object URL')
    try:
        target = urlsplit(object_id)
    except ValueError as exc:
        raise ValueError('invalid canonical feed object URL') from exc
    if (target.scheme not in ('http', 'https') or not target.hostname
            or target.username or target.password or any(ch.isspace() for ch in object_id)):
        raise ValueError('invalid canonical feed object URL')
    return object_id


def feed_author_header(name: str, actor_id: str) -> str:
    """Reuse Community-style author attribution without untrusted Markdown controls."""
    # Backtick code spans are the Community convention; raw name backticks
    # would terminate the span and make a forged mention/header possible.
    safe_name = re.sub(r'[\r\n\t`<>]', '', str(name or '')).strip()
    return format_fediverse_author_for_discord(safe_name or 'Remote user', actor_id)


def _utf16_units(value: str) -> int:
    """Count UTF-16 code units, including surrogate pairs for Unicode emoji."""
    return len(value.encode('utf-16-le')) // 2


def _prefix_for_budget(value: str, units: int) -> int:
    """Return the last complete Unicode codepoint fitting the Discord budget."""
    count = 0
    for index, char in enumerate(value):
        count += 2 if ord(char) > 0xffff else 1
        if count > units:
            return index
    return len(value)


def render_feed_discord_message(*, header: str, body: str,
                                footer: str | None = None) -> str:
    """Bound one bridge-owned feed message without breaking protected links.

    Mandatory author/identity layout is reserved first. If a URL or supported
    Markdown link crosses the body cutoff, drop the whole fragment and tail;
    never emit a partial Discord reconciliation key.
    """
    if not header.strip():
        raise ValueError('feed author header must not be empty')
    suffix = f'\n\n{footer}' if footer else ''
    head = header.strip()
    mandatory = head + suffix
    if _utf16_units(mandatory) > DISCORD_MESSAGE_LIMIT:
        raise ValueError('mandatory feed author/identity exceeds Discord limit')
    clean_body = str(body or '').strip()
    if not clean_body:
        return mandatory
    formatted = head + '\n\n' + clean_body + suffix
    if _utf16_units(formatted) <= DISCORD_MESSAGE_LIMIT:
        return formatted
    # Reserve both separators, the source footer and an ellipsis before the
    # body. A zero-budget body is omitted without truncating the footer.
    budget = DISCORD_MESSAGE_LIMIT - _utf16_units(head + '\n\n' + suffix)
    if budget <= 0:
        return mandatory
    cut = _prefix_for_budget(clean_body, max(0, budget - 1))
    for link in _FEED_LINK.finditer(clean_body):
        if link.start() < cut < link.end():
            cut = link.start()
            break
    # Prefer a nearby word boundary, but never force a complete long word to
    # disappear when a safe character-prefix suffices.
    whitespace = max(clean_body.rfind(' ', 0, cut), clean_body.rfind('\n', 0, cut))
    if whitespace > 0 and cut - whitespace < 32:
        cut = whitespace
    prefix = clean_body[:cut].rstrip()
    short_body = (prefix + '…') if prefix else '…'
    result = head + '\n\n' + short_body + suffix
    if _utf16_units(result) > DISCORD_MESSAGE_LIMIT:
        raise ValueError('feed message exceeds Discord length after truncation')
    return result


def format_lemmy_post_for_discord(
    author: str,
    title: str,
    body: str,
    url: str,
    actor_id: str = "",
) -> str:
    """Render one remote Lemmy post for Discord thread starter content."""
    display = format_fediverse_author_for_discord(author, actor_id)
    parts = [f"**{title.strip()}**", f"Author: {display}"]
    if body.strip():
        parts.append(body.strip())
    parts.append(url)
    return truncate("\n\n".join(parts), DISCORD_MESSAGE_LIMIT)


def format_lemmy_comment_for_discord(
    author: str, body: str, url: str, actor_id: str = ""
) -> str:
    """Render one remote Lemmy comment for Discord message content."""
    # Show full fediverse handle when we can derive the instance domain from actor_id.
    # Falls back to plain username when actor_id is absent (e.g. synthetic events).
    parts = [format_fediverse_author_for_discord(author, actor_id)]
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
    return truncate(
        normalize_text(title) or "Untitled Lemmy Post", DISCORD_THREAD_NAME_LIMIT
    )
