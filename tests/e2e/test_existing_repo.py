"""Test 2: Existing repo gets embedded dev system."""

import subprocess
from collections.abc import Callable
from pathlib import Path

from genesis.scaffold import SEED_AGENTS, scaffold_existing_repo


GOAL = "Add authentication and deploy to cloud"
PROJECT = "myapp"

EXISTING_FILES = {
    "README.md": "# My App\n\nA simple web app.\n",
    "src/main.py": "def main():\n    print('hello')\n",
    "requirements.txt": "flask==3.0.0\n",
}


def test_existing_repo_preserves_original_files(
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    repo = init_test_repo(PROJECT, EXISTING_FILES)
    scaffold_existing_repo(repo, GOAL, PROJECT)

    # Original files untouched
    assert (repo / "src" / "main.py").read_text() == EXISTING_FILES["src/main.py"]
    assert (repo / "requirements.txt").read_text() == EXISTING_FILES["requirements.txt"]


def test_existing_repo_appends_to_claude_md(
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    # Repo that already has a CLAUDE.md
    files_with_claude_md = {
        **EXISTING_FILES,
        "CLAUDE.md": "# My App\n\nExisting instructions here.\n",
    }
    repo = init_test_repo(PROJECT, files_with_claude_md)
    scaffold_existing_repo(repo, GOAL, PROJECT)

    claude_md = (repo / "CLAUDE.md").read_text()
    # The repo's own instructions stay where they were, and gain the import that
    # brings the genesis rules in from AGENTS.md.
    assert "Existing instructions here." in claude_md
    assert "@AGENTS.md" in claude_md
    assert GOAL in (repo / "AGENTS.md").read_text()


def test_existing_repo_appends_to_agents_md(
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    repo = init_test_repo(PROJECT, {**EXISTING_FILES, "AGENTS.md": "# My App\n\nCodex notes.\n"})
    scaffold_existing_repo(repo, GOAL, PROJECT)

    agents_md = (repo / "AGENTS.md").read_text()
    assert "Codex notes." in agents_md
    assert "Genesis Dev System" in agents_md and GOAL in agents_md


def test_existing_repo_import_is_not_added_twice(
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    repo = init_test_repo(PROJECT, {**EXISTING_FILES, "CLAUDE.md": "@AGENTS.md\n\nMine.\n"})
    scaffold_existing_repo(repo, GOAL, PROJECT)
    assert (repo / "CLAUDE.md").read_text().count("@AGENTS.md") == 1


def test_existing_repo_keeps_its_protected_paths_and_adds_codex_ones(
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    repo = init_test_repo(PROJECT, {**EXISTING_FILES, ".genesis/protected-paths": "src/billing/\n.claude/\n"})
    scaffold_existing_repo(repo, GOAL, PROJECT, engine="codex")
    lines = (repo / ".genesis" / "protected-paths").read_text().splitlines()
    assert "src/billing/" in lines
    assert lines.count(".claude/") == 1
    assert ".codex/" in lines


def test_existing_repo_creates_claude_md_if_missing(
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    repo = init_test_repo(PROJECT, EXISTING_FILES)
    scaffold_existing_repo(repo, GOAL, PROJECT)

    agents_md = (repo / "AGENTS.md").read_text()
    assert GOAL in agents_md
    assert "Genesis Dev System" not in agents_md  # No separator needed for new file
    assert (repo / "CLAUDE.md").read_text().startswith("@AGENTS.md")


def test_existing_repo_has_seed_agents(
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    repo = init_test_repo(PROJECT, EXISTING_FILES)
    scaffold_existing_repo(repo, GOAL, PROJECT)

    for agent in SEED_AGENTS:
        assert (repo / ".claude" / "agents" / f"{agent}.md").exists()


def test_existing_repo_has_workflows(
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    repo = init_test_repo(PROJECT, EXISTING_FILES)
    scaffold_existing_repo(repo, GOAL, PROJECT)

    assert (repo / ".github" / "workflows" / "genesis-orchestrator.yml").exists()
    assert (repo / ".github" / "workflows" / "genesis-events.yml").exists()
    assert (repo / ".github" / "workflows" / "genesis-evolver.yml").exists()


def test_existing_repo_has_settings_with_hooks(
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    import json

    repo = init_test_repo(PROJECT, EXISTING_FILES)
    scaffold_existing_repo(repo, GOAL, PROJECT)

    settings = json.loads((repo / ".claude" / "settings.json").read_text())
    assert "hooks" in settings
    assert "PostToolUse" in settings["hooks"]
    assert "log.sh" in settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]


def test_existing_repo_has_genesis_config(
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    repo = init_test_repo(PROJECT, EXISTING_FILES)
    scaffold_existing_repo(repo, GOAL, PROJECT)

    config = (repo / ".genesis" / "config.toml").read_text()
    assert PROJECT in config
    assert GOAL in config


def test_existing_repo_has_scripts(
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    repo = init_test_repo(PROJECT, EXISTING_FILES)
    scaffold_existing_repo(repo, GOAL, PROJECT)

    assert (repo / ".genesis" / "scripts" / "log.sh").exists()
    assert (repo / ".genesis" / "scripts" / "issues.sh").exists()


def test_existing_repo_has_onboarding_issue(
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    repo = init_test_repo(PROJECT, EXISTING_FILES)
    scaffold_existing_repo(repo, GOAL, PROJECT)

    onboarding = (repo / ".genesis" / "onboarding.md").read_text()
    assert GOAL in onboarding


def test_existing_repo_creates_new_commit(
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    repo = init_test_repo(PROJECT, EXISTING_FILES)
    scaffold_existing_repo(repo, GOAL, PROJECT)

    result = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline"],
        capture_output=True,
        text=True,
    )
    lines = result.stdout.strip().split("\n")
    assert len(lines) == 2  # Original commit + genesis commit
    assert "genesis" in lines[0].lower()
    assert "Initial commit" in lines[1]


def test_existing_repo_rejects_non_git_dir(tmp_dir: Path) -> None:
    not_a_repo = tmp_dir / "not-a-repo"
    not_a_repo.mkdir()

    import pytest

    with pytest.raises(ValueError, match="not a git repository"):
        scaffold_existing_repo(not_a_repo, GOAL, PROJECT)
