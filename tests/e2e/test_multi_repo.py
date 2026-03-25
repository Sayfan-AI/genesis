"""Test 3: Multi-repo goal with external dev repo."""

import subprocess
from collections.abc import Callable
from pathlib import Path

from genesis.scaffold import SEED_AGENTS, scaffold_external_dev_repo


GOAL = "Migrate both repos to use gRPC"
PROJECT = "grpc-migration"


def test_multi_repo_creates_dev_repo(
    tmp_dir: Path,
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    repo_a = init_test_repo("repo-a", {"README.md": "# Repo A\n", "src/app.py": "# app\n"})
    repo_b = init_test_repo("repo-b", {"README.md": "# Repo B\n", "lib/utils.rs": "// utils\n"})

    dev_repo = tmp_dir / "dev-repo"
    scaffold_external_dev_repo(dev_repo, [str(repo_a), str(repo_b)], GOAL, PROJECT)

    assert (dev_repo / ".git").is_dir()

    result = subprocess.run(
        ["git", "-C", str(dev_repo), "log", "--oneline"],
        capture_output=True,
        text=True,
    )
    assert "Initial scaffold by genesis" in result.stdout


def test_multi_repo_claude_md_references_targets(
    tmp_dir: Path,
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    repo_a = init_test_repo("repo-a", {"README.md": "# Repo A\n"})
    repo_b = init_test_repo("repo-b", {"README.md": "# Repo B\n"})

    dev_repo = tmp_dir / "dev-repo"
    scaffold_external_dev_repo(dev_repo, [str(repo_a), str(repo_b)], GOAL, PROJECT)

    claude_md = (dev_repo / "CLAUDE.md").read_text()
    assert "Target Repositories" in claude_md
    assert str(repo_a) in claude_md
    assert str(repo_b) in claude_md


def test_multi_repo_onboarding_references_targets(
    tmp_dir: Path,
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    repo_a = init_test_repo("repo-a", {"README.md": "# Repo A\n"})
    repo_b = init_test_repo("repo-b", {"README.md": "# Repo B\n"})

    dev_repo = tmp_dir / "dev-repo"
    scaffold_external_dev_repo(dev_repo, [str(repo_a), str(repo_b)], GOAL, PROJECT)

    onboarding = (dev_repo / ".genesis" / "onboarding.md").read_text()
    assert str(repo_a) in onboarding
    assert str(repo_b) in onboarding
    assert GOAL in onboarding


def test_multi_repo_has_full_scaffold(
    tmp_dir: Path,
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    repo_a = init_test_repo("repo-a", {"README.md": "# Repo A\n"})
    repo_b = init_test_repo("repo-b", {"README.md": "# Repo B\n"})

    dev_repo = tmp_dir / "dev-repo"
    scaffold_external_dev_repo(dev_repo, [str(repo_a), str(repo_b)], GOAL, PROJECT)

    # Full scaffold present
    for agent in SEED_AGENTS:
        assert (dev_repo / ".claude" / "agents" / f"{agent}.md").exists()
    assert (dev_repo / ".github" / "workflows" / "genesis-orchestrator.yml").exists()
    assert (dev_repo / ".github" / "workflows" / "genesis-events.yml").exists()
    assert (dev_repo / ".claude" / "settings.json").exists()
    assert (dev_repo / "README.md").exists()


def test_multi_repo_has_genesis_config_with_targets(
    tmp_dir: Path,
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    repo_a = init_test_repo("repo-a", {"README.md": "# Repo A\n"})
    repo_b = init_test_repo("repo-b", {"README.md": "# Repo B\n"})

    dev_repo = tmp_dir / "dev-repo"
    scaffold_external_dev_repo(dev_repo, [str(repo_a), str(repo_b)], GOAL, PROJECT)

    config = (dev_repo / ".genesis" / "config.toml").read_text()
    assert PROJECT in config
    assert str(repo_a) in config
    assert str(repo_b) in config
    assert "[targets]" in config


def test_multi_repo_has_hooks(
    tmp_dir: Path,
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    import json

    repo_a = init_test_repo("repo-a", {"README.md": "# Repo A\n"})
    repo_b = init_test_repo("repo-b", {"README.md": "# Repo B\n"})

    dev_repo = tmp_dir / "dev-repo"
    scaffold_external_dev_repo(dev_repo, [str(repo_a), str(repo_b)], GOAL, PROJECT)

    settings = json.loads((dev_repo / ".claude" / "settings.json").read_text())
    assert "hooks" in settings
    assert "SessionStart" in settings["hooks"]


def test_multi_repo_does_not_modify_targets(
    tmp_dir: Path,
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    repo_a = init_test_repo("repo-a", {"README.md": "# Repo A\n", "src/app.py": "# app\n"})
    repo_b = init_test_repo("repo-b", {"README.md": "# Repo B\n", "lib/utils.rs": "// utils\n"})

    # Record state before
    def get_commit_hash(repo: Path) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    hash_a_before = get_commit_hash(repo_a)
    hash_b_before = get_commit_hash(repo_b)

    dev_repo = tmp_dir / "dev-repo"
    scaffold_external_dev_repo(dev_repo, [str(repo_a), str(repo_b)], GOAL, PROJECT)

    # Target repos completely untouched
    assert get_commit_hash(repo_a) == hash_a_before
    assert get_commit_hash(repo_b) == hash_b_before
    assert (repo_a / "src" / "app.py").read_text() == "# app\n"
    assert (repo_b / "lib" / "utils.rs").read_text() == "// utils\n"
    assert not (repo_a / ".claude").exists()
    assert not (repo_b / ".claude").exists()
