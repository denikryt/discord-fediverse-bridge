"""Effective bridge policy normalization and request-scoped evaluation.

The service merges immutable environment bootstrap entries with active database
rows. It does not authorize slash commands by itself; DiscordOps preconditions
consume snapshots produced here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlsplit


class PolicyType(StrEnum):
    """Supported dynamic bridge policy categories."""

    FEDERATION_ALLOW = "federation_allow"
    FEDERATION_BLOCK = "federation_block"
    DISCORD_GUILD_ALLOW = "discord_guild_allow"
    DISCORD_GUILD_BLOCK = "discord_guild_block"
    BRIDGE_SUPER_ADMIN = "bridge_super_admin"


class FederationPolicyReason(StrEnum):
    """Explain one federation admission decision without reparsing policy."""

    ALLOWED = "allowed"
    BLOCKLISTED = "blocklisted"
    NOT_ALLOWLISTED = "not_allowlisted"
    INVALID_HOST = "invalid_host"


@dataclass(frozen=True, slots=True)
class EffectivePolicyEntry:
    """Expose one normalized effective entry and its immutable source."""

    policy_type: PolicyType
    subject: str
    source: Literal["bootstrap", "dynamic"]


@dataclass(frozen=True, slots=True)
class FederationPolicyDecision:
    """Represent one federation allow/block decision."""

    allowed: bool
    reason: FederationPolicyReason
    host: str | None


def normalize_snowflake(value: int | str) -> str:
    """Return a canonical decimal Discord snowflake or raise ValueError."""
    text = str(value)
    if not text or not text.isdecimal():
        raise ValueError("Discord IDs must contain decimal digits only.")
    return text


def normalize_instance_subject(value: str) -> str:
    """Normalize a hostname or URL to canonical DNS/IP host text.

    Any whitespace is rejected because silently trimming policy subjects can
    hide deployment or operator mistakes.
    """
    if not value or any(ch.isspace() for ch in value):
        raise ValueError("Instance subject must not contain whitespace.")
    parsed = urlsplit(value if "://" in value else f"//{value}")
    host = parsed.hostname
    if not host:
        raise ValueError("Instance subject must contain a valid hostname.")
    host = host.rstrip(".")
    try:
        return ip_address(host).compressed.lower()
    except ValueError:
        try:
            normalized = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("Instance subject contains an invalid hostname.") from exc
        if not normalized or any(not label for label in normalized.split(".")):
            raise ValueError("Instance subject contains an invalid hostname.")
        return normalized


@dataclass(frozen=True, slots=True)
class BridgePolicySnapshot:
    """Immutable effective policy reused throughout one action or fanout batch."""

    entries: tuple[EffectivePolicyEntry, ...]

    def _subjects(self, policy_type: PolicyType) -> frozenset[str]:
        return frozenset(entry.subject for entry in self.entries if entry.policy_type is policy_type)

    def is_super_admin(self, discord_user_id: str) -> bool:
        """Return whether one Discord user is an effective bridge administrator."""
        try:
            subject = normalize_snowflake(discord_user_id)
        except ValueError:
            return False
        return subject in self._subjects(PolicyType.BRIDGE_SUPER_ADMIN)

    def is_discord_guild_allowed(self, guild_id: int | str | None) -> bool:
        """Apply block-first and unrestricted-empty-allowlist guild policy."""
        if guild_id is None:
            return True
        try:
            subject = normalize_snowflake(guild_id)
        except ValueError:
            return False
        blocked = self._subjects(PolicyType.DISCORD_GUILD_BLOCK)
        allowed = self._subjects(PolicyType.DISCORD_GUILD_ALLOW)
        if subject in blocked:
            return False
        return not allowed or subject in allowed

    def federation_decision(self, url_or_host: str) -> FederationPolicyDecision:
        """Apply block-first and unrestricted-empty-allowlist federation policy."""
        try:
            host = normalize_instance_subject(url_or_host)
        except ValueError:
            return FederationPolicyDecision(False, FederationPolicyReason.INVALID_HOST, None)
        blocked = self._subjects(PolicyType.FEDERATION_BLOCK)
        allowed = self._subjects(PolicyType.FEDERATION_ALLOW)
        if host in blocked:
            return FederationPolicyDecision(False, FederationPolicyReason.BLOCKLISTED, host)
        if allowed and host not in allowed:
            return FederationPolicyDecision(False, FederationPolicyReason.NOT_ALLOWLISTED, host)
        return FederationPolicyDecision(True, FederationPolicyReason.ALLOWED, host)

    def list_effective_entries(self, policy_type: PolicyType) -> list[EffectivePolicyEntry]:
        """Return deterministic effective entries for private management output."""
        return sorted(
            (entry for entry in self.entries if entry.policy_type is policy_type),
            key=lambda entry: entry.subject,
        )


class BridgePolicyService:
    """Merge bootstrap settings and active policy rows into immutable snapshots."""

    def __init__(self, *, settings: object, repository: object) -> None:
        self.settings = settings
        self.repository = repository

    def snapshot(self) -> BridgePolicySnapshot:
        """Read active rows once and merge them with normalized bootstrap policy."""
        bootstrap_values = {
            PolicyType.FEDERATION_ALLOW: getattr(self.settings, "federation_allowlist", []),
            PolicyType.FEDERATION_BLOCK: getattr(self.settings, "federation_blocklist", []),
            PolicyType.DISCORD_GUILD_ALLOW: getattr(self.settings, "discord_guild_allowlist", []),
            PolicyType.DISCORD_GUILD_BLOCK: getattr(self.settings, "discord_guild_blocklist", []),
            PolicyType.BRIDGE_SUPER_ADMIN: getattr(self.settings, "bridge_super_admin_user_ids", []),
        }
        merged: dict[tuple[PolicyType, str], EffectivePolicyEntry] = {}
        for policy_type, values in bootstrap_values.items():
            for raw in values:
                subject = self.normalize_subject(policy_type, str(raw))
                merged[(policy_type, subject)] = EffectivePolicyEntry(policy_type, subject, "bootstrap")
        for row in self.repository.list_all_active():
            policy_type = PolicyType(str(row.policy_type))
            key = (policy_type, str(row.normalized_subject))
            merged.setdefault(key, EffectivePolicyEntry(policy_type, key[1], "dynamic"))
        return BridgePolicySnapshot(tuple(sorted(merged.values(), key=lambda item: (item.policy_type.value, item.subject))))

    @staticmethod
    def normalize_subject(policy_type: PolicyType, subject: str) -> str:
        """Normalize a subject according to its policy category."""
        if policy_type in {PolicyType.FEDERATION_ALLOW, PolicyType.FEDERATION_BLOCK}:
            return normalize_instance_subject(subject)
        return normalize_snowflake(subject)


def bridge_policy_service_for(*, settings: object, database: object, existing: BridgePolicyService | None = None) -> BridgePolicyService:
    """Return an existing policy service or build one from runtime dependencies.

    Older test/runtime harnesses may not expose the new service explicitly.
    Constructing it here preserves one policy implementation without requiring
    every lightweight harness to mirror the full production Runtime object.
    """
    if existing is not None:
        return existing
    return BridgePolicyService(
        settings=settings,
        repository=database.bridge_policy_entries,
    )


def runtime_bridge_policy_service(runtime: object, *, database: object | None = None) -> BridgePolicyService:
    """Resolve the policy service from a production or lightweight runtime.

    Production wiring supplies ``bridge_policy_service``. Scenario harnesses
    built before dynamic policy management can still derive the same service
    from their existing ``settings`` and ``database`` attributes.
    """
    existing = getattr(runtime, "bridge_policy_service", None)
    settings = getattr(runtime, "settings", None)
    if settings is None:
        # Legacy scenario harnesses omit Settings entirely. Empty bootstrap
        # lists preserve the documented unrestricted defaults for tests.
        settings = type("RuntimePolicySettings", (), {})()
    resolved_database = database or getattr(runtime, "database", None)
    if resolved_database is None:
        raise RuntimeError("Runtime policy resolution requires a database.")
    return bridge_policy_service_for(
        settings=settings,
        database=resolved_database,
        existing=existing,
    )
