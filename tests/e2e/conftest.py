"""Shared fixtures for e2e tests."""

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    """Provide a temporary directory for test repos."""
    return tmp_path


@pytest.fixture
def init_test_repo(tmp_dir: Path) -> Callable[[str, dict[str, str]], Path]:
    """Factory fixture that creates a git repo with given files and an initial commit."""

    def _create(name: str, files: dict[str, str]) -> Path:
        repo_path = tmp_dir / name
        repo_path.mkdir(parents=True)

        subprocess.run(["git", "init", str(repo_path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo_path), "config", "user.email", "test@test.com"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_path), "config", "user.name", "Test"],
            check=True,
            capture_output=True,
        )

        for rel_path, content in files.items():
            file_path = repo_path / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content)

        subprocess.run(
            ["git", "-C", str(repo_path), "add", "-A"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_path), "commit", "-m", "Initial commit"],
            check=True,
            capture_output=True,
        )

        return repo_path

    return _create
