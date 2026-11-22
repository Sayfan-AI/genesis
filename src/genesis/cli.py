"""Command-line interface for genesis."""

from __future__ import annotations

import argparse
import os
import sys

from genesis.fleet import (
    fleet_logs,
    fleet_restart,
    fleet_resume,
    fleet_status,
    fleet_stop,
    fleet_sync,
    load_fleet_config,
)
from genesis.server import serve
from genesis.watch import run_watch
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
        "--engine",
        choices=["claude", "codex"],
        help=(
            "Coding agent that runs the sessions. Defaults to the [engine] name "
            "in .genesis/config.toml if the repo sets one, and to claude otherwise."
        ),
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
        "--all-workflows",
        action="store_true",
        help=(
            "Disable every workflow, not just the genesis-* ones. Off by default "
            "so CI and other gates keep running while the local plane drives."
        ),
    )
    serve_parser.add_argument(
        "--supervised",
        action="store_true",
        help=(
            "Supervisor mode (used by launchd): honor .genesis/.stopped instead "
            "of clearing it on startup."
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

    fleet_parser = subparsers.add_parser(
        "fleet",
        help="Manage launchd supervisors for local genesis serve planes.",
    )
    fleet_parser.add_argument(
        "--config",
        help="Path to fleet.toml (default: ~/.config/genesis/fleet.toml, env: GENESIS_FLEET_CONFIG)",
    )
    fleet_sub = fleet_parser.add_subparsers(dest="fleet_command", required=True)
    fleet_sub.add_parser("status", help="Show live state across all configured repos.")
    fleet_sub.add_parser(
        "sync",
        help="Render and load/unload LaunchAgents to match fleet.toml.",
    )
    for cmd_name, cmd_help in (
        ("resume", "Clear .genesis/.stopped and start/kickstart a stopped plane."),
        ("start", "Alias for `resume`."),
        ("stop", "Write .genesis/.stopped and send SIGTERM."),
        ("restart", "Clear .genesis/.stopped and kickstart the LaunchAgent."),
    ):
        p = fleet_sub.add_parser(cmd_name, help=cmd_help)
        p.add_argument(
            "target",
            nargs="?",
            default="all",
            help="Fleet repo name or 'all' (default: all)",
        )
    logs_parser = fleet_sub.add_parser("logs", help="Tail .genesis/.serve.stdout for a repo.")
    logs_parser.add_argument("name", help="Fleet repo name")
    logs_parser.add_argument(
        "-n",
        "--lines",
        type=int,
        default=50,
        help="Number of trailing lines to print (default: 50)",
    )

    watch_parser = subparsers.add_parser(
        "watch",
        help="Check fleet planes and GitHub boards for silent stalls.",
    )
    watch_parser.add_argument(
        "--config",
        help="Path to fleet.toml (default: ~/.config/genesis/fleet.toml)",
    )
    watch_parser.add_argument(
        "--repo-dir",
        help="Check a single checkout instead of reading fleet.toml.",
    )
    watch_parser.add_argument(
        "--phantom-minutes",
        type=float,
        default=5.0,
        help="Minutes before an unbacked in-progress issue is treated as stranded (default: 5)",
    )
    watch_parser.add_argument(
        "--hung-minutes",
        type=float,
        default=20.0,
        help="Minutes without tool calls before a running session is flagged hung (default: 20)",
    )
    watch_parser.add_argument(
        "--starve-minutes",
        type=float,
        default=15.0,
        help="Minutes before idle selectable work triggers a nudge/alert (default: 15)",
    )
    watch_parser.add_argument(
        "--renotify-minutes",
        type=float,
        default=30.0,
        help="Minutes between repeat aggregate alerts when unescalated (default: 30)",
    )
    watch_parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Suppress desktop and Loki notifications.",
    )
    watch_parser.add_argument(
        "--no-remediate",
        action="store_true",
        help="Report findings without auto-releasing phantom claims or writing .nudge.",
    )
    watch_parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit structured JSON output.",
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
        if args.engine:
            os.environ["GENESIS_ENGINE"] = args.engine
        if args.all_workflows:
            os.environ["GENESIS_ALL_WORKFLOWS"] = "1"
        if args.supervised:
            os.environ["GENESIS_SUPERVISED"] = "1"
        return serve()

    if args.command == "workflows":
        if args.workflows_command == "enable":
            enable_workflows(repo=args.repo)
            return 0
        if args.workflows_command == "disable":
            disable_workflows(repo=args.repo)
            return 0

    if args.command == "fleet":
        try:
            cfg = load_fleet_config(args.config)
            if args.fleet_command == "status":
                return fleet_status(cfg)
            if args.fleet_command == "sync":
                return fleet_sync(cfg)
            if args.fleet_command in ("resume", "start"):
                return fleet_resume(cfg, args.target)
            if args.fleet_command == "stop":
                return fleet_stop(cfg, args.target)
            if args.fleet_command == "restart":
                return fleet_restart(cfg, args.target)
            if args.fleet_command == "logs":
                return fleet_logs(cfg, args.name, lines=args.lines)
        except (FileNotFoundError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    if args.command == "watch":
        try:
            return run_watch(
                config_path=args.config,
                repo_dir=args.repo_dir,
                phantom_minutes=args.phantom_minutes,
                hung_minutes=args.hung_minutes,
                starve_minutes=args.starve_minutes,
                renotify_minutes=args.renotify_minutes,
                notify=not args.no_notify,
                remediate=not args.no_remediate,
                as_json=args.as_json,
            )
        except (FileNotFoundError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
