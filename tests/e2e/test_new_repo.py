"""Test 1: New repo with embedded dev system."""

import subprocess
from pathlib import Path

from genesis.scaffold import SEED_AGENTS, scaffold_new_repo


GOAL = "Build a CLI tool that converts markdown to PDF"
PROJECT = "md2pdf"


def test_new_repo_creates_git_repo(tmp_dir: Path) -> None:
    repo = tmp_dir / PROJECT
    scaffold_new_repo(repo, GOAL, PROJECT)

    assert (repo / ".git").is_dir()

    result = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline"],
        capture_output=True,
        text=True,
    )
    assert "Initial scaffold by genesis" in result.stdout


def test_new_repo_has_claude_md(tmp_dir: Path) -> None:
    repo = tmp_dir / PROJECT
    scaffold_new_repo(repo, GOAL, PROJECT)

    claude_md = (repo / "CLAUDE.md").read_text()
    assert PROJECT in claude_md
    assert GOAL in claude_md
    # Meta-concepts
    assert "Deterministic over agentic" in claude_md
    assert "Incremental planning" in claude_md
    assert "Self-improvement" in claude_md
    assert "Quality gates" in claude_md


def test_new_repo_has_readme(tmp_dir: Path) -> None:
    repo = tmp_dir / PROJECT
    scaffold_new_repo(repo, GOAL, PROJECT)

    readme = (repo / "README.md").read_text()
    assert PROJECT in readme
    assert GOAL in readme


def test_new_repo_has_seed_agents(tmp_dir: Path) -> None:
    repo = tmp_dir / PROJECT
    scaffold_new_repo(repo, GOAL, PROJECT)

    agents_dir = repo / ".claude" / "agents"
    assert agents_dir.is_dir()
    for agent in SEED_AGENTS:
        agent_file = agents_dir / f"{agent}.md"
        assert agent_file.exists(), f"Missing agent: {agent}"
        content = agent_file.read_text()
        assert len(content) > 0


def test_new_repo_has_workflows(tmp_dir: Path) -> None:
    repo = tmp_dir / PROJECT
    scaffold_new_repo(repo, GOAL, PROJECT)

    workflows_dir = repo / ".github" / "workflows"
    assert (workflows_dir / "genesis-orchestrator.yml").exists()
    assert (workflows_dir / "genesis-events.yml").exists()


def test_new_repo_has_settings_with_hooks(tmp_dir: Path) -> None:
    repo = tmp_dir / PROJECT
    scaffold_new_repo(repo, GOAL, PROJECT)

    import json

    settings = json.loads((repo / ".claude" / "settings.json").read_text())
    hooks = settings["hooks"]
    # Verify all expected hook events are configured
    expected_hooks = [
        "SessionStart",
        "SessionEnd",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "SubagentStart",
        "SubagentStop",
        "UserPromptSubmit",
    ]
    for hook in expected_hooks:
        assert hook in hooks, f"Missing hook: {hook}"
        assert len(hooks[hook]) > 0
        assert "genctl log" in hooks[hook][0]["command"]


def test_new_repo_has_genesis_config(tmp_dir: Path) -> None:
    repo = tmp_dir / PROJECT
    scaffold_new_repo(repo, GOAL, PROJECT)

    config = (repo / ".genesis" / "config.toml").read_text()
    assert PROJECT in config
    assert GOAL in config
    assert "[loki]" in config
    assert "[issues]" in config
    assert "[a2h]" in config


def test_new_repo_has_onboarding_issue(tmp_dir: Path) -> None:
    repo = tmp_dir / PROJECT
    scaffold_new_repo(repo, GOAL, PROJECT)

    onboarding = (repo / ".genesis" / "onboarding.md").read_text()
    assert GOAL in onboarding
    assert PROJECT in onboarding
    assert "milestone" in onboarding.lower()


def test_new_repo_no_target_repos_in_claude_md(tmp_dir: Path) -> None:
    repo = tmp_dir / PROJECT
    scaffold_new_repo(repo, GOAL, PROJECT)

    claude_md = (repo / "CLAUDE.md").read_text()
    assert "Target Repositories" not in claude_md
