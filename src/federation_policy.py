"""Federation allowlist policy for inbound ActivityPub events and subscribe commands."""

from __future__ import annotations

from urllib.parse import urlparse


def is_instance_allowed(url: str, allowlist: list[str]) -> bool:
    """Return whether the instance hosting *url* is permitted by *allowlist*.

    An empty allowlist means all instances are allowed (open federation).
    A non-empty allowlist restricts federation to the listed hostnames only.
    The check is case-insensitive and strips port numbers — only the bare
    hostname is compared against each entry.
    """
    # Empty allowlist = open federation, no restriction.
    if not allowlist:
        return True

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False

    return hostname in {entry.strip().lower() for entry in allowlist}
