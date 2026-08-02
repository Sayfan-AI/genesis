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
    serve_parser.add_argument(
        "--agent",
        help=(
            "Agent definition to run (default: .claude/agents/orchestrator.md, "
            "env: GENESIS_AGENT). Repos without an orchestrator — genesis itself, "
            "for one — point this at the agent they actually have."
        ),
    )
    serve_parser.add_argument(
        "--personal-profile",
        action="store_true",
        help=(
            "Run agent sessions under your own Claude Code profile instead of the "
            "isolated agent one (env: GENESIS_CLAUDE_PROFILE=personal). By default "
            "sessions use a separate profile so they don't inherit your personal "
            "~/.claude/CLAUDE.md as if it were agent policy."
        ),
    )
    serve_parser.add_argument(
        "--claude-home",
        help=(
            "Config dir for the agent's Claude Code profile "
            "(default: ~/.config/genesis/claude-home, env: GENESIS_CLAUDE_HOME)"
        ),
    )
    serve_parser.add_argument(
        "--all-workflows",
        action="store_true",
        help=(
            "Disable every workflow, not just the genesis-* ones. Off by default "
            "so CI and other gates keep running while the local plane drives."
        ),
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
        if args.agent:
            os.environ["GENESIS_AGENT"] = args.agent
        if args.all_workflows:
            os.environ["GENESIS_ALL_WORKFLOWS"] = "1"
        if args.personal_profile:
            os.environ["GENESIS_CLAUDE_PROFILE"] = "personal"
        if args.claude_home:
            os.environ["GENESIS_CLAUDE_HOME"] = args.claude_home
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
