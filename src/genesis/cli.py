"""Command-line interface for genesis."""

from __future__ import annotations

import argparse
import os
import sys

from genesis.server import serve
from genesis.workflows import disable_workflows, enable_workflows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="genesis",
        description="Genesis: bootstrapper for autonomous agentic AI dev systems",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser(
        "serve",
        help="Run the orchestrator locally (local control plane).",
        description=(
            "Run the orchestrator agent locally. Disables GitHub Actions "
            "workflows in the repo to prevent duplicate runs and re-enables "
            "them on graceful shutdown."
        ),
    )
    serve_parser.add_argument(
        "--repo",
        help="owner/repo to manage (default: detected via gh from git remote)",
    )
    serve_parser.add_argument(
        "--poll-interval",
        type=int,
        help="Seconds between event polls (default: 60, env: GENESIS_POLL_INTERVAL)",
    )
    serve_parser.add_argument(
        "--session-timeout",
        type=int,
        help="Max seconds per orchestrator session (default: 3600, env: GENESIS_SESSION_TIMEOUT)",
    )

    workflows_parser = subparsers.add_parser(
        "workflows",
        help="Manage GitHub Actions workflows for the current repo.",
    )
    workflows_subparsers = workflows_parser.add_subparsers(
        dest="workflows_command", required=True
    )
    enable_parser = workflows_subparsers.add_parser(
        "enable", help="Enable manually-disabled workflows."
    )
    enable_parser.add_argument(
        "--repo",
        help="owner/repo to manage (default: detected from cwd's git remote)",
    )
    disable_parser = workflows_subparsers.add_parser(
        "disable", help="Disable all currently-active workflows."
    )
    disable_parser.add_argument(
        "--repo",
        help="owner/repo to manage (default: detected from cwd's git remote)",
    )

    args = parser.parse_args(argv)

    if args.command == "serve":
        if args.repo:
            os.environ["GENESIS_REPO"] = args.repo
        if args.poll_interval is not None:
            os.environ["GENESIS_POLL_INTERVAL"] = str(args.poll_interval)
        if args.session_timeout is not None:
            os.environ["GENESIS_SESSION_TIMEOUT"] = str(args.session_timeout)
        return serve()

    if args.command == "workflows":
        if args.workflows_command == "enable":
            enable_workflows(repo=args.repo)
            return 0
        if args.workflows_command == "disable":
            disable_workflows(repo=args.repo)
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
