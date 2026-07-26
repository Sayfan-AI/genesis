"""Tests for workflow template content."""

import re
from pathlib import Path

import pytest

from genesis.scaffold import (
    ORCHESTRATOR_TURN_FLOOR,
    SEED_WORKFLOWS,
    TEMPLATES_DIR,
    WORKFLOW_TURN_CLASSES,
    scaffold_new_repo,
)

REPO_ROOT = TEMPLATES_DIR.parent
MAX_TURNS_RE = re.compile(r"--max-turns\s+(\d+)")


def _claude_workflows(directory: Path) -> list[Path]:
    """Workflow files in `directory` that actually invoke Claude."""
    return [
        wf
        for wf in sorted(directory.glob("*.yml"))
        if "anthropics/claude-code-action" in wf.read_text()
    ]


def test_orchestrator_workflow_uses_claude_action() -> None:
    content = (TEMPLATES_DIR / "workflows" / "genesis-orchestrator.yml").read_text()
    assert "anthropics/claude-code-action@v1" in content
    assert "ANTHROPIC_API_KEY" in content
    assert "cron:" in content
    assert "workflow_dispatch" in content


def test_events_workflow_uses_claude_action() -> None:
    content = (TEMPLATES_DIR / "workflows" / "genesis-events.yml").read_text()
    assert "anthropics/claude-code-action@v1" in content
    assert "ANTHROPIC_API_KEY" in content
    assert "issues:" in content
    assert "pull_request:" in content
    assert "issue_comment:" in content


def test_evolver_workflow_uses_claude_action() -> None:
    content = (TEMPLATES_DIR / "workflows" / "genesis-evolver.yml").read_text()
    assert "anthropics/claude-code-action@v1" in content
    assert "ANTHROPIC_API_KEY" in content
    assert "cron:" in content
    assert "workflow_dispatch" in content
    assert "evolver" in content


def test_events_workflow_skips_bot_events() -> None:
    content = (TEMPLATES_DIR / "workflows" / "genesis-events.yml").read_text()
    assert "github-actions[bot]" in content


def test_workflows_have_correct_permissions() -> None:
    for name in ["genesis-orchestrator.yml", "genesis-events.yml", "genesis-evolver.yml"]:
        content = (TEMPLATES_DIR / "workflows" / name).read_text()
        assert "contents: write" in content
        assert "issues: write" in content
        assert "pull-requests: write" in content


def test_every_claude_workflow_template_is_classified() -> None:
    """A new Claude-invoking template must join a turn-budget class, or it
    silently escapes the floor guard below."""
    found = {wf.name for wf in _claude_workflows(TEMPLATES_DIR / "workflows")}
    assert found == set(WORKFLOW_TURN_CLASSES), (
        "templates/workflows and WORKFLOW_TURN_CLASSES disagree: "
        f"unclassified={sorted(found - set(WORKFLOW_TURN_CLASSES))}, "
        f"stale={sorted(set(WORKFLOW_TURN_CLASSES) - found)}"
    )


@pytest.mark.parametrize("name", sorted(WORKFLOW_TURN_CLASSES))
def test_workflow_template_declares_turn_budget_for_its_class(name: str) -> None:
    """Every Claude-invoking template declares an explicit `--max-turns`, at or
    above its class floor.

    A run that dies at `error_max_turns` produces no progress and no diagnosis,
    and the next run redoes the work from scratch. Orchestrator-class agents get
    headroom; narrow fixed-procedure agents stay small on purpose so needing more
    turns fails fast instead of wandering.
    """
    content = (TEMPLATES_DIR / "workflows" / name).read_text()
    match = MAX_TURNS_RE.search(content)
    assert match, f"{name} invokes Claude with no explicit --max-turns"
    turns = int(match.group(1))

    if WORKFLOW_TURN_CLASSES[name] == "orchestrator":
        assert turns >= ORCHESTRATOR_TURN_FLOOR, (
            f"{name} is orchestrator-class but budgets only {turns} turns "
            f"(floor {ORCHESTRATOR_TURN_FLOOR})"
        )
    else:
        assert turns < ORCHESTRATOR_TURN_FLOOR, (
            f"{name} is narrow-class but budgets {turns} turns. The fix for a "
            "starved fixed-procedure run is a tighter procedure, not a bigger "
            "budget — reclassify it if it genuinely became open-ended."
        )


def test_genesis_own_claude_workflows_meet_orchestrator_floor() -> None:
    """Genesis's own agent workflows are orchestrator-class too — they were the
    tightest-budgeted agents in the system while doing the broadest work."""
    workflows = _claude_workflows(REPO_ROOT / ".github" / "workflows")
    assert workflows, "no Claude-invoking workflows found in .github/workflows"
    for wf in workflows:
        match = MAX_TURNS_RE.search(wf.read_text())
        assert match, f"{wf.name} invokes Claude with no explicit --max-turns"
        turns = int(match.group(1))
        assert turns >= ORCHESTRATOR_TURN_FLOOR, (
            f"{wf.name} budgets only {turns} turns (floor {ORCHESTRATOR_TURN_FLOOR})"
        )


def test_scaffolded_workflows_match_templates(tmp_dir: Path) -> None:
    repo = tmp_dir / "test-project"
    scaffold_new_repo(repo, "test goal", "test-project")

    for name in SEED_WORKFLOWS:
        template = (TEMPLATES_DIR / "workflows" / name).read_text()
        scaffolded = (repo / ".github" / "workflows" / name).read_text()
        assert scaffolded == template
