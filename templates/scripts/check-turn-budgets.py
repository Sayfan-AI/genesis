#!/usr/bin/env python3
"""Fail when a workflow invokes Claude without a turn budget its class allows.

Usage: check-turn-budgets.py [WORKFLOWS_DIR]   (default: .github/workflows)

A run that dies at `error_max_turns` is the worst failure shape a dev system
has: no progress, no diagnosis, and the next run redoes the work. So every step
that invokes Claude must pass an explicit `--max-turns`, and an
orchestrator-class workflow must give it at least ORCHESTRATOR_FLOOR turns.
`genesis-guards.yml` runs this on every pull request, and the auto-merge only
lands a pull request whose checks all passed, so a budget regression can't merge.

Genesis seeds this because every dev system was told to write the check itself,
and each one did, from scratch, after its workflows had already gone unguarded
(genesis issue #184). It's stdlib-only and reads the YAML as text, so it runs on
any runner with no install step, whatever language the project uses.

Extend ORCHESTRATOR_CLASS when you add an open-ended Claude workflow. Anything
not listed is narrow-class: a fixed procedure that still needs an explicit
budget, kept small so a run that needs more turns fails fast.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ORCHESTRATOR_FLOOR = 30

# Workflow file stems that run open-ended agent work. Dispatched subagents spend
# from the same budget, which is why these need headroom.
ORCHESTRATOR_CLASS = {
    "genesis-orchestrator",
    "genesis-events",
    "genesis-evolver",
    "genesis-ci-failure",
}

CLAUDE_ACTION = "anthropics/claude-code-action"
# `claude` as a command word, not `.claude/` or `claude-dir-guard.sh`.
CLAUDE_CLI = re.compile(r"(?:^|[\s;&|(])claude(?:\s|$)")
MAX_TURNS = re.compile(r"--max-turns[=\s]+(\S+)")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _strip_comment(line: str) -> str:
    if line.lstrip().startswith("#"):
        return ""
    return re.sub(r"\s+#.*$", "", line)


def steps(source: str) -> list[str]:
    """Each step under any `steps:` key, as its comment-free text."""
    lines = [_strip_comment(line) for line in source.splitlines()]
    found: list[str] = []
    i = 0
    while i < len(lines):
        match = re.match(r"^(\s*)steps:\s*$", lines[i])
        i += 1
        if not match:
            continue
        key_indent = len(match.group(1))
        item_indent: int | None = None
        current: list[str] | None = None
        while i < len(lines):
            line = lines[i]
            if line.strip() and _indent(line) <= key_indent and not (
                line.lstrip().startswith("- ") and _indent(line) == key_indent
            ):
                break
            if line.strip() and line.lstrip().startswith("- "):
                if item_indent is None:
                    item_indent = _indent(line)
                if _indent(line) == item_indent:
                    if current is not None:
                        found.append("\n".join(current))
                    current = []
            if current is not None:
                current.append(line)
            i += 1
        if current is not None:
            found.append("\n".join(current))
    return found


def _step_name(step: str) -> str:
    match = re.search(r"^\s*-?\s*name:\s*(.+)$", step, re.MULTILINE)
    return match.group(1).strip().strip("'\"") if match else "(unnamed step)"


def invokes_claude(step: str) -> bool:
    return CLAUDE_ACTION in step or bool(CLAUDE_CLI.search(step))


def check_workflow(stem: str, source: str) -> list[str]:
    """Problems with one workflow's turn budgets; empty when it passes."""
    problems: list[str] = []
    claude_steps = [s for s in steps(source) if invokes_claude(s)]
    orchestrator = stem in ORCHESTRATOR_CLASS
    if orchestrator and not claude_steps:
        problems.append(
            "is listed in ORCHESTRATOR_CLASS but no step invokes Claude: remove "
            "it from the list, or the scanner has stopped seeing its steps"
        )
    for step in claude_steps:
        name = _step_name(step)
        match = MAX_TURNS.search(step)
        if not match:
            problems.append(f"step '{name}' invokes Claude without --max-turns")
            continue
        value = match.group(1).strip("'\"")
        if not value.isdigit():
            problems.append(
                f"step '{name}' sets --max-turns to {value!r}, which this check "
                "can't read as a number of turns"
            )
            continue
        if orchestrator and int(value) < ORCHESTRATOR_FLOOR:
            problems.append(
                f"step '{name}' budgets {value} turns, below the "
                f"orchestrator-class floor of {ORCHESTRATOR_FLOOR}"
            )
    return problems


def main(argv: list[str]) -> int:
    directory = Path(argv[1]) if len(argv) > 1 else Path(".github/workflows")
    if not directory.is_dir():
        print(f"check-turn-budgets: no workflows directory at {directory}")
        return 0
    failed = False
    checked = 0
    for path in sorted(directory.glob("*.y*ml")):
        source = path.read_text()
        checked += sum(1 for s in steps(source) if invokes_claude(s))
        for problem in check_workflow(path.stem, source):
            failed = True
            print(f"{path}: {problem}", file=sys.stderr)
    if failed:
        print(
            "check-turn-budgets: fix the budget, or classify the workflow in "
            ".genesis/scripts/check-turn-budgets.py. Raise a whole class when a "
            "run dies at max-turns, not one workflow.",
            file=sys.stderr,
        )
        return 1
    print(f"check-turn-budgets: {checked} Claude step(s) in {directory} pass")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
