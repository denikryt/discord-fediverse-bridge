"""Guard against reintroducing retired local ActivityPub URL families.

Stage 7 is a cleanup boundary, not a runtime routing change. This test scans
repository text at test time so old local actor aliases cannot return through
production route registrations, shared helpers, or current-behavior gateway
fixtures. Remote-origin URLs and explicit legacy migration inputs remain valid
because those are external identities or upgrade fixtures, not local bridge
contracts.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Historical plans are records, not current contracts. Migration tests keep old
# local URLs as legacy database input, and parser tests may assert rejection of
# retired local paths. Those files are excluded by path instead of by broad
# pattern so new current-behavior fixtures still get scanned.
EXCLUDED_PREFIXES = (
    "plans/",
    ".git/",
    "node_modules/",
    "fedify-gateway/node_modules/",
)
EXCLUDED_FILES = {
    "tests/test_stage4_identity_migration.py",
    "tests/test_legacy_identity_cutover.py",
    "tests/test_stage5_exact_stage4_migration.py",
    "tests/test_activitypub_identity_urls.py",
    "fedify-gateway/tests/verify-activitypub-identity-urls.ts",
    "fedify-gateway/tests/verify-stage4-public-cutover.ts",
    "fedify-gateway/tests/verify-user-outbox.ts",
    "tests/test_stage7_no_old_local_activitypub_urls.py",
}

SCANNED_PREFIXES = (
    "src/",
    "fedify-gateway/src/",
    "fedify-gateway/tests/",
    "tests/",
    "docs/",
    "dev/",
    "notes/",
    "deploy/",
)
SCANNED_FILES = {
    "README.md",
    "compose.yaml",
    "compose.nginx.yaml",
    "fedify-gateway/SETUP.md",
    "tests/behavior/test_personal_feed_stage6_control_plane.py",
    "tests/test_phase9_bidirectional_mirror_messages.py",
    "tests/test_user_bans_plan93.py",
}

RETIRED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "old public ActivityPub Hono route registration",
        re.compile(r'app\.(?:get|post|use)\("/(?:users|communities|c|activitypub)(?:/|"|:)'),
    ),
    (
        "old untyped local actor Hono route registration",
        re.compile(r'app\.(?:get|post|use)\("/actors/:identifier(?:/|"|:)'),
    ),
    (
        "same-origin local /users actor or object URL",
        re.compile(r'https://(?:bridge\.example|bridge\.test|discord-bridge\.example\.com)(?::\d+)?/users/'),
    ),
    (
        "same-origin local /communities actor URL",
        re.compile(r'https://(?:bridge\.example|bridge\.test|discord-bridge\.example\.com)(?::\d+)?/communities/'),
    ),
    (
        "same-origin local /c actor URL",
        re.compile(r'https://(?:bridge\.example|bridge\.test|discord-bridge\.example\.com)(?::\d+)?/c/'),
    ),
    (
        "same-origin untyped /actors URL",
        re.compile(r'https://(?:bridge\.example|bridge\.test|discord-bridge\.example\.com)(?::\d+)?/actors/(?!services/|users/|communities/)[A-Za-z0-9_{}$-]'),
    ),
    (
        "same-origin root-level local /activities URL",
        re.compile(r'https://(?:bridge\.example|bridge\.test|discord-bridge\.example\.com)(?::\d+)?/activities/'),
    ),
    (
        "BRIDGE_HOST_DOMAIN root-level local /activities URL",
        re.compile(r'https://\{BRIDGE_HOST_DOMAIN\}/activities/'),
    ),
    (
        "BRIDGE_HOST_DOMAIN old local /users URL",
        re.compile(r'https://\{BRIDGE_HOST_DOMAIN\}/users/'),
    ),
    (
        "BRIDGE_HOST_DOMAIN untyped /actors URL",
        re.compile(r'https://\{BRIDGE_HOST_DOMAIN\}/actors/\{[^}/]+\}'),
    ),
)

DOC_RETIREMENT_WORDS = (
    "retired",
    "unsupported",
    "not compatibility",
    "not an activitypub actor alias",
    "not activitypub actor aliases",
    "not act as an activitypub actor alias",
    "must not act as an activitypub actor alias",
    "must stay unsupported",
    "browser-only",
)


def _tracked_files() -> list[str]:
    """Return git-tracked paths so generated files never affect the guard."""
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.splitlines()


def _should_scan(path: str) -> bool:
    """Classify source-like files whose current contracts should be guarded."""
    if path in EXCLUDED_FILES or path.endswith(('.png', '.jpg', '.jpeg', '.gif')):
        return False
    if any(path.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
        return False
    return path in SCANNED_FILES or any(path.startswith(prefix) for prefix in SCANNED_PREFIXES)


def _is_allowed_documentation_retirement(path: str, line: str) -> bool:
    """Allow docs to name old paths only when the same line says they are retired."""
    if not (path.startswith("docs/") or path.startswith("dev/") or path.startswith("notes/")):
        return False
    lowered = line.lower()
    return any(word in lowered for word in DOC_RETIREMENT_WORDS)


def test_no_old_local_activitypub_url_construction() -> None:
    """Fail when current source or fixtures construct retired local AP URLs."""
    failures: list[str] = []
    for path in _tracked_files():
        if not _should_scan(path):
            continue
        text_path = ROOT / path
        try:
            lines = text_path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(lines, start=1):
            if _is_allowed_documentation_retirement(path, line):
                continue
            for label, pattern in RETIRED_PATTERNS:
                if pattern.search(line):
                    failures.append(f"{path}:{line_number}: {label}: {line.strip()}")
    assert not failures, "Retired local ActivityPub URL construction found:\n" + "\n".join(failures)
