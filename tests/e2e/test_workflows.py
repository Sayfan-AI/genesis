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


def test_ci_workflow_runs_the_suite_on_every_pr_without_secrets() -> None:
    """The guards in this file are only guards if something runs them.

    Before ci.yml existed, the whole suite ran only when a human remembered to,
    which made every assertion here advisory. Two properties keep it that way:
    it must fire on `pull_request`, and it must need no secrets — a CI job that
    costs money or an API key is one someone eventually disables.
    """
    content = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "pull_request:" in content
    # Match the commands, not the file. The comments in ci.yml discuss --frozen
    # at length to explain why it is *not* used, and scanning raw text flags
    # that prose — same reason the secrets check below matches on interpolation.
    commands = "\n".join(
        line
        for line in content.splitlines()
        if line.strip().lstrip("- ").startswith("run:")
    )
    # --locked, not --frozen: --frozen installs a stale lock without complaint,
    # so a dependency added to pyproject.toml without a re-lock would pass CI.
    assert "uv sync --locked" in commands, (
        "CI must install with --locked so a stale uv.lock fails the run"
    )
    assert "uv run --no-sync pytest" in commands, (
        "CI must run tests with --no-sync so the test step cannot touch the lock"
    )
    assert "--frozen" not in commands, (
        "--frozen is not the guard it looks like: it accepts a stale lock, and "
        "it does not keep uv off the configured index (build-system.requires is "
        "resolved outside the lock). Use --locked + --no-sync."
    )
    # A workflow can only consume a secret through this interpolation, so match
    # on it rather than the bare word — prose in a comment is not a secret.
    assert "${{ secrets." not in content, (
        "CI must not consume secrets — it has to run on every PR for free"
    )


def test_scaffolded_workflows_match_templates(tmp_dir: Path) -> None:
    repo = tmp_dir / "test-project"
    scaffold_new_repo(repo, "test goal", "test-project")

    for name in SEED_WORKFLOWS:
        template = (TEMPLATES_DIR / "workflows" / name).read_text()
        scaffolded = (repo / ".github" / "workflows" / name).read_text()
        assert scaffolded == template


def test_template_action_pins_match_genesis_own_workflows() -> None:
    """Templates must not rot behind the actions genesis runs on itself.

    Dependabot's `github-actions` ecosystem only scans `.github/workflows/`.
    `templates/workflows/` is invisible to it, so every scaffolded dev repo kept
    getting `actions/checkout@v4` — pinned to a Node 20 runtime GitHub retired
    in June 2026 — while genesis's own workflows were bumped to v7 by a
    Dependabot PR nobody realised didn't cover the templates.

    Pinning templates to whatever genesis runs on itself makes the Dependabot PR
    that bumps `.github/workflows/` fail this test until the templates move too,
    which is the only automated pressure the template directory gets.
    """
    action_re = re.compile(r"uses:\s*([\w.-]+/[\w.-]+)@(\S+)")

    def pins(directory: Path) -> dict[str, set[str]]:
        found: dict[str, set[str]] = {}
        for wf in sorted(directory.glob("*.yml")):
            for action, version in action_re.findall(wf.read_text()):
                found.setdefault(action, set()).add(version)
        return found

    own = pins(REPO_ROOT / ".github" / "workflows")
    template = pins(TEMPLATES_DIR / "workflows")

    for action, versions in sorted(template.items()):
        if action not in own:
            # Only actions genesis also uses can be cross-checked; the rest are
            # covered by the single-version assertion below.
            continue
        assert versions == own[action], (
            f"templates/workflows pins {action} at {sorted(versions)} but "
            f".github/workflows pins it at {sorted(own[action])}. Dependabot "
            f"does not see templates/workflows — bump it by hand in the same PR."
        )

    for action, versions in sorted(template.items()):
        assert len(versions) == 1, (
            f"templates/workflows pins {action} at more than one version "
            f"({sorted(versions)}); a scaffolded repo should be internally consistent"
        )
