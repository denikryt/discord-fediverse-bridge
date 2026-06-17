"""One-shot profile-media orphan cleanup using existing ownership locks."""

from __future__ import annotations

import argparse
from pathlib import Path

from .db import Database
from .profile_media import ProfileMediaStore
from .user_profiles import UserProfileService


def build_parser() -> argparse.ArgumentParser:
    """Build a cleanup-only parser whose destructive action is explicit."""
    parser = argparse.ArgumentParser(prog="bridge-profile-media")
    subcommands = parser.add_subparsers(dest="command", required=True)
    cleanup = subcommands.add_parser("cleanup")
    cleanup.add_argument("--database-url", required=True)
    cleanup.add_argument("--media-root", type=Path, required=True)
    cleanup.add_argument("--deployment-lock", type=Path, required=True)
    cleanup.add_argument("--media-lock", type=Path, required=True)
    cleanup.add_argument("--grace-seconds", type=int, default=86400)
    cleanup.add_argument("--lock-timeout-seconds", type=float, default=30.0)
    return parser


def _validate_cleanup_paths(args: argparse.Namespace) -> ProfileMediaStore:
    """Reject unsafe cleanup configuration before opening the database."""
    if args.grace_seconds < 0:
        raise SystemExit("--grace-seconds must be non-negative")
    if args.lock_timeout_seconds <= 0:
        raise SystemExit("--lock-timeout-seconds must be positive")
    store = ProfileMediaStore(
        args.media_root,
        max_upload_bytes=1,
        max_pixels=1,
    )
    # Initialization owns symlink, ownership, and write/access validation for
    # every media directory used by deletion.
    store.initialize()
    for lock_path in (args.deployment_lock, args.media_lock):
        if lock_path.is_symlink() or lock_path.parent.is_symlink():
            raise SystemExit("lock paths must not be symlinks")
    return store


def main(argv: list[str] | None = None) -> int:
    """Delete only old unreferenced media after preflight validation."""
    args = build_parser().parse_args(argv)
    store = _validate_cleanup_paths(args)
    database = Database(args.database_url)
    try:
        service = UserProfileService(database, store)
        count = service.cleanup_orphans(
            grace_seconds=args.grace_seconds,
            deployment_lock_path=args.deployment_lock,
            media_lock_path=args.media_lock,
            lock_timeout_seconds=args.lock_timeout_seconds,
        )
        print(count)
        return 0
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
