"""Local operator CLI for bounded User Actor delivery diagnostics and retry."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from .db import Database

EXIT_SUCCESS = 0
EXIT_NOT_FOUND = 3
EXIT_OWNERSHIP_MISMATCH = 4
EXIT_NOT_RETRYABLE = 5


def build_parser() -> argparse.ArgumentParser:
    """Build the explicit-selector administrative command parser."""
    parser = argparse.ArgumentParser(prog="bridge-user-deliveries")
    parser.add_argument("--database-url", required=True)
    subcommands = parser.add_subparsers(dest="command", required=True)

    listing = subcommands.add_parser("list")
    listing.add_argument("--username")
    listing.add_argument("--status")
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--json", action="store_true")

    retry = subcommands.add_parser("retry")
    retry.add_argument("--delivery-id", type=int)
    retry.add_argument("--source-id", type=int)
    retry.add_argument("--username")
    retry.add_argument("--all-failed", action="store_true")
    retry.add_argument("--limit", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one database-only diagnostic or retry operation."""
    args = build_parser().parse_args(argv)
    if args.command == "retry":
        exact = sum(value is not None for value in (args.delivery_id, args.source_id))
        if args.all_failed:
            if exact or not args.username or args.limit is None:
                raise SystemExit("bulk retry requires --username --all-failed --limit")
        elif exact != 1 or args.limit is not None:
            raise SystemExit(
                "retry requires exactly one of --delivery-id or --source-id"
            )

    database = Database(args.database_url)
    try:
        repository = database.user_actor_deliveries
        if args.command == "list":
            result = repository.list_diagnostics(
                username=args.username,
                status=args.status,
                limit=args.limit,
            )
            payload = [asdict(item) for item in result]
            print(json.dumps(payload, default=str, sort_keys=True))
            return EXIT_SUCCESS
        if args.all_failed:
            result = repository.retry_failed_batch(
                username=args.username,
                limit=args.limit,
            )
        elif args.delivery_id is not None:
            result = repository.retry_failed_by_delivery_id(
                args.delivery_id,
                args.username,
            )
        else:
            result = repository.retry_failed_by_source_id(
                args.source_id,
                args.username,
            )
        print(json.dumps(asdict(result), default=str, sort_keys=True))
        return {
            "success": EXIT_SUCCESS,
            "not_found": EXIT_NOT_FOUND,
            "ownership_mismatch": EXIT_OWNERSHIP_MISMATCH,
            "not_retryable": EXIT_NOT_RETRYABLE,
        }[result.outcome]
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
