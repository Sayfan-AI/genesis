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
    assert (workflows_dir / "genesis-evolver.yml").exists()


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
        # Format: [{"matcher": "", "hooks": [{"type": "command", "command": "..."}]}]
        # Every event logs, but don't assume logging is the FIRST entry. This
        # assertion used to read inner[0] and broke the moment PreToolUse gained
        # the host-guard in front of the logger, which is a test failing on
        # ordering rather than on behavior.
        commands = [h["command"] for entry in hooks[hook] for h in entry["hooks"]]
        assert any("log.sh" in c for c in commands), f"{hook} does not log: {commands}"

    # The host-guard is what keeps a session out of ~/.ssh and friends, and it is
    # inert unless it is declared here. It also has to precede the logger on
    # PreToolUse: a guard that runs after the call it was meant to block is a
    # comment.
    pre = [h["command"] for entry in hooks["PreToolUse"] for h in entry["hooks"]]
    assert any("host-guard.sh" in c for c in pre), f"host-guard not wired: {pre}"
    guard_at = next(i for i, c in enumerate(pre) if "host-guard.sh" in c)
    log_at = next(i for i, c in enumerate(pre) if "log.sh" in c)
    assert guard_at < log_at, f"host-guard must run before the logger: {pre}"

    # Same reasoning for the .claude/ gate. It is the only thing that turns an
    # unactionable "you haven't granted it yet" into a named request, and it is a
    # file on disk doing nothing at all unless it is declared here — which is
    # exactly the class of failure it exists to report (#49).
    assert any("claude-dir-guard.sh" in c for c in pre), f".claude gate not wired: {pre}"
    gate_at = next(i for i, c in enumerate(pre) if "claude-dir-guard.sh" in c)
    assert gate_at < log_at, f"the .claude gate must run before the logger: {pre}"


def test_new_repo_has_genesis_config(tmp_dir: Path) -> None:
    repo = tmp_dir / PROJECT
    scaffold_new_repo(repo, GOAL, PROJECT)

    config = (repo / ".genesis" / "config.toml").read_text()
    assert PROJECT in config
    assert GOAL in config
    assert "[issues]" in config
    assert "[a2h]" in config
    # Loki is env/secrets only — the token is a secret and this file is committed.
    assert "[loki]" not in config


def test_new_repo_workflows_forward_loki_secrets(tmp_dir: Path) -> None:
    """Every workflow that runs Claude must pass the Loki creds through the
    action's `settings` env block, or every Actions run loses its activity
    trail — hook stderr lands in Claude Code's transcript, not the run log."""
    repo = tmp_dir / PROJECT
    scaffold_new_repo(repo, GOAL, PROJECT)

    workflows_dir = repo / ".github" / "workflows"
    for wf in sorted(workflows_dir.glob("genesis-*.yml")):
        content = wf.read_text()
        if "anthropics/claude-code-action" not in content:
            continue
        for var in ("GENESIS_LOKI_URL", "GENESIS_LOKI_USER", "GENESIS_LOKI_TOKEN"):
            assert f'"{var}": "${{{{ secrets.{var} }}}}"' in content, (
                f"{wf.name} does not forward {var} to the logging hooks"
            )


def test_new_repo_has_scripts(tmp_dir: Path) -> None:
    repo = tmp_dir / PROJECT
    scaffold_new_repo(repo, GOAL, PROJECT)

    scripts_dir = repo / ".genesis" / "scripts"
    assert (scripts_dir / "log.sh").exists()
    assert (scripts_dir / "issues.sh").exists()
    assert (scripts_dir / "activate.sh").exists()
    # Scripts should be executable
    import os
    assert os.access(scripts_dir / "log.sh", os.X_OK)
    assert os.access(scripts_dir / "issues.sh", os.X_OK)
    assert os.access(scripts_dir / "activate.sh", os.X_OK)


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
