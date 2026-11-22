"""The seeded turn-budget check (genesis issue #184).

Every dev system was told to write this check and each one did, by hand, after
its workflows had already gone unguarded: genesis-dashboard PR #12, then ALPHA
issue #18. Genesis now seeds it as `.genesis/scripts/check-turn-budgets.py` and
runs it from `genesis-guards.yml`. These tests hold it to the templates it
guards, to genesis's own classes and floor, and to each regression it exists
to catch.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from genesis.scaffold import (
    ORCHESTRATOR_TURN_FLOOR,
    SEED_SCRIPTS,
    SEED_WORKFLOWS,
    TEMPLATES_DIR,
    WORKFLOW_TURN_CLASSES,
    scaffold_new_repo,
)

SCRIPT = TEMPLATES_DIR / "scripts" / "check-turn-budgets.py"
WORKFLOWS = TEMPLATES_DIR / "workflows"


def _load():
    spec = importlib.util.spec_from_file_location("check_turn_budgets", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # No __pycache__ under templates/: the scaffold copies that tree, and other
    # tests read every file in it as text.
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


check = _load()

ACTION_STEP = """\
name: Example
on: workflow_dispatch
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - name: Run Claude
        uses: anthropics/claude-code-action@v1
        with:
          claude_args: "{args}"
      - name: After
        run: echo done
"""

CLI_STEP = """\
name: Example
on: workflow_dispatch
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - name: Ask Claude
        run: |
          claude -p "triage this" {args}
"""


def _run(directory: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(directory)],
        capture_output=True,
        text=True,
    )


def test_every_seeded_workflow_passes() -> None:
    result = _run(WORKFLOWS)
    assert result.returncode == 0, result.stderr


def test_it_sees_every_claude_step_genesis_classifies() -> None:
    """If the scanner went blind, every workflow would pass."""
    for name in WORKFLOW_TURN_CLASSES:
        source = (WORKFLOWS / name).read_text()
        claude_steps = [s for s in check.steps(source) if check.invokes_claude(s)]
        assert len(claude_steps) == 1, f"{name}: found {len(claude_steps)} Claude steps"
        assert "--max-turns" in claude_steps[0]


def test_it_carries_genesis_own_classes_and_floor() -> None:
    """The script and WORKFLOW_TURN_CLASSES are a pair: the seed must enforce
    the floor genesis holds its templates to, on the same workflows."""
    assert check.ORCHESTRATOR_FLOOR == ORCHESTRATOR_TURN_FLOOR
    orchestrator = {
        Path(name).stem
        for name, kind in WORKFLOW_TURN_CLASSES.items()
        if kind == "orchestrator"
    }
    assert check.ORCHESTRATOR_CLASS == orchestrator


@pytest.mark.parametrize("template", [ACTION_STEP, CLI_STEP], ids=["action", "cli"])
def test_a_claude_step_without_a_budget_fails(template: str) -> None:
    problems = check.check_workflow("narrow-thing", template.format(args="--allowedTools Read"))
    assert len(problems) == 1 and "without --max-turns" in problems[0]


def test_an_orchestrator_below_the_floor_fails() -> None:
    source = ACTION_STEP.format(args="--max-turns 20")
    problems = check.check_workflow("genesis-events", source)
    assert len(problems) == 1 and "below the orchestrator-class floor of 30" in problems[0]
    assert "Run Claude" in problems[0]


def test_the_floor_applies_by_class_only() -> None:
    source = ACTION_STEP.format(args="--max-turns 20")
    assert check.check_workflow("something-narrow", source) == []
    assert check.check_workflow("genesis-events", ACTION_STEP.format(args="--max-turns 30")) == []


def test_a_budget_only_in_a_comment_does_not_count() -> None:
    source = ACTION_STEP.format(args="--allowedTools Read").replace(
        "          claude_args:", "          # pass --max-turns 40 here\n          claude_args:"
    )
    assert check.check_workflow("genesis-events", source) != []


def test_a_budget_in_a_later_step_does_not_cover_the_claude_step() -> None:
    source = ACTION_STEP.format(args="--allowedTools Read").replace(
        "run: echo done", "run: echo --max-turns 40"
    )
    assert check.check_workflow("narrow-thing", source) != []


def test_a_budget_it_cannot_read_fails() -> None:
    source = ACTION_STEP.format(args="--max-turns ${{ vars.TURNS }}")
    problems = check.check_workflow("narrow-thing", source)
    assert len(problems) == 1 and "can't read" in problems[0]


def test_paths_and_script_names_are_not_claude_invocations() -> None:
    source = CLI_STEP.format(args="").replace(
        'claude -p "triage this" ', "bash .genesis/scripts/claude-dir-guard.sh .claude/x"
    )
    assert check.check_workflow("narrow-thing", source) == []


def test_an_orchestrator_workflow_that_stopped_invoking_claude_fails() -> None:
    source = ACTION_STEP.replace("anthropics/claude-code-action@v1", "actions/hello@v1")
    problems = check.check_workflow("genesis-orchestrator", source)
    assert len(problems) == 1 and "no step invokes Claude" in problems[0]


def test_the_cli_exits_non_zero_and_names_the_file(tmp_path: Path) -> None:
    (tmp_path / "genesis-events.yml").write_text(ACTION_STEP.format(args="--max-turns 10"))
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "genesis-events.yml" in result.stderr


def test_the_seeded_guard_workflow_runs_the_seeded_script() -> None:
    assert "check-turn-budgets.py" in SEED_SCRIPTS
    assert "genesis-guards.yml" in SEED_WORKFLOWS
    content = (WORKFLOWS / "genesis-guards.yml").read_text()
    assert "python3 .genesis/scripts/check-turn-budgets.py .github/workflows" in content
    assert "pull_request:" in content
    assert "${{ secrets." not in content, "the guard must run before the App exists"


def test_a_scaffolded_repo_passes_its_own_guard(tmp_dir: Path) -> None:
    repo = tmp_dir / "test-project"
    scaffold_new_repo(repo, "test goal", "test-project")
    script = repo / ".genesis" / "scripts" / "check-turn-budgets.py"
    assert script.stat().st_mode & 0o111
    result = subprocess.run(
        [sys.executable, str(script), ".github/workflows"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "4 Claude step(s)" in result.stdout
