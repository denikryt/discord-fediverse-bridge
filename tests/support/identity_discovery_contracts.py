"""Typed contracts for identity normalization, discovery, and labels."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Literal

Action = Literal[
    "normalize_handle", "extract_handle", "resolve_community", "relay_label"
]


@dataclass(frozen=True, slots=True)
class IdentityDiscoveryExpected:
    value: str | None = None
    source: str | None = None
    error_contains: str | None = None


@dataclass(frozen=True, slots=True)
class IdentityDiscoveryCase:
    id: str
    action: Action
    raw: str
    expected: IdentityDiscoveryExpected


IDENTITY_DISCOVERY_CASES = (
    IdentityDiscoveryCase(
        "handle.normalize.canonical",
        "normalize_handle",
        " Alice@Example.COM ",
        IdentityDiscoveryExpected("Alice@example.com"),
    ),
    IdentityDiscoveryCase(
        "handle.normalize.reject_url",
        "normalize_handle",
        "https://example.com/u/alice",
        IdentityDiscoveryExpected(error_contains="invalid"),
    ),
    IdentityDiscoveryCase(
        "handle.extract.users_path",
        "extract_handle",
        "https://Example.COM/users/Alice/",
        IdentityDiscoveryExpected("Alice@example.com"),
    ),
    IdentityDiscoveryCase(
        "handle.extract.unknown_none",
        "extract_handle",
        "https://example.com/",
        IdentityDiscoveryExpected(None),
    ),
    IdentityDiscoveryCase(
        "community.resolve.actor_url",
        "resolve_community",
        "https://lemmy.world/c/technology",
        IdentityDiscoveryExpected("https://lemmy.world/c/technology", "remote_lemmy"),
    ),
    IdentityDiscoveryCase(
        "community.resolve.handle",
        "resolve_community",
        "!technology@lemmy.world",
        IdentityDiscoveryExpected("https://lemmy.world/c/technology", "remote_lemmy"),
    ),
    IdentityDiscoveryCase(
        "community.resolve.encoded",
        "resolve_community",
        "lemmy:https://lemmy.world/c/technology|technology|42",
        IdentityDiscoveryExpected("https://lemmy.world/c/technology", "remote_lemmy"),
    ),
    IdentityDiscoveryCase(
        "community.resolve.plain_ambiguous",
        "resolve_community",
        "technology",
        IdentityDiscoveryExpected(
            error_contains="Select a community from autocomplete"
        ),
    ),
    IdentityDiscoveryCase(
        "label.stored_handle",
        "relay_label",
        "!hackers@lemmy.example",
        IdentityDiscoveryExpected("hackers@lemmy.example"),
    ),
    IdentityDiscoveryCase(
        "label.actor_url",
        "relay_label",
        "https://lemmy.world/c/technology",
        IdentityDiscoveryExpected("technology@lemmy.world"),
    ),
)


@dataclass(frozen=True, slots=True)
class RequiredRule:
    id: str
    description: str
    represented_by: tuple[str, ...]


REQUIRED_IDENTITY_DISCOVERY_RULES = (
    RequiredRule(
        "handle_normalization",
        "Displayed remote handles normalize canonically.",
        ("handle.normalize.canonical",),
    ),
    RequiredRule(
        "handle_rejects_urls",
        "Remote-handle input rejects actor URLs.",
        ("handle.normalize.reject_url",),
    ),
    RequiredRule(
        "actor_url_extraction",
        "Known actor paths yield best-effort handles.",
        ("handle.extract.users_path", "handle.extract.unknown_none"),
    ),
    RequiredRule(
        "self_contained_resolution",
        "Actor URLs, handles, and encoded choices resolve without instance input.",
        (
            "community.resolve.actor_url",
            "community.resolve.handle",
            "community.resolve.encoded",
        ),
    ),
    RequiredRule(
        "plain_name_ambiguous",
        "Plain names without an instance are rejected as ambiguous.",
        ("community.resolve.plain_ambiguous",),
    ),
    RequiredRule(
        "relay_labels",
        "Stored handles and actor URLs render compact labels.",
        ("label.stored_handle", "label.actor_url"),
    ),
)
