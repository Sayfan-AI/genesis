"""GitHub integration for genesis.

Handles repo creation, pushing, and issue management via the gh CLI.
"""

import json
import subprocess
from pathlib import Path


class GitHubError(Exception):
    """Raised when a GitHub operation fails."""


def _run_gh(args: list[str], cwd: Path | None = None) -> str:
    """Run a gh CLI command and return stdout."""
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise GitHubError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _run_git(repo_path: Path, *args: str) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitHubError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def create_github_repo(
    project_name: str,
    org: str | None = None,
    private: bool = True,
) -> str:
    """Create a new GitHub repo. Returns the repo URL."""
    repo_name = f"{org}/{project_name}" if org else project_name
    args = ["repo", "create", repo_name, "--confirm"]
    if private:
        args.append("--private")
    else:
        args.append("--public")

    _run_gh(args)

    # Get the repo URL
    owner = org if org else _run_gh(["api", "user", "--jq", ".login"])
    return f"https://github.com/{owner}/{project_name}"


def push_to_github(repo_path: Path, repo_url: str) -> None:
    """Add remote and push the repo to GitHub."""
    # Check if remote already exists
    try:
        _run_git(repo_path, "remote", "get-url", "origin")
    except GitHubError:
        _run_git(repo_path, "remote", "add", "origin", f"{repo_url}.git")

    _run_git(repo_path, "push", "-u", "origin", "main")


def open_onboarding_issue(repo_path: Path) -> str:
    """Create the onboarding issue (#1) from .genesis/onboarding.md. Returns issue URL."""
    onboarding_path = repo_path / ".genesis" / "onboarding.md"
    if not onboarding_path.exists():
        raise GitHubError(f"No onboarding file found at {onboarding_path}")

    content = onboarding_path.read_text()

    # Extract the title from the first heading
    title = "Onboarding"
    for line in content.splitlines():
        if line.startswith("# "):
            title = line.removeprefix("# ").strip()
            break

    result = _run_gh(
        [
            "issue", "create",
            "--title", title,
            "--body", content,
            "--label", "genesis:onboarding",
        ],
        cwd=repo_path,
    )

    return result  # gh issue create prints the issue URL


def publish_to_github(
    path: Path,
    project_name: str,
    goal: str,
    org: str | None = None,
    private: bool = True,
) -> str:
    """Full publish flow: create repo, push, open issue #1. Returns repo URL."""
    # Ensure the branch is named main
    try:
        _run_git(path, "branch", "-M", "main")
    except GitHubError:
        pass  # Already on main

    repo_url = create_github_repo(project_name, org=org, private=private)
    push_to_github(path, repo_url)

    # Create the onboarding label first (ignore if it already exists)
    try:
        _run_gh(
            ["label", "create", "genesis:onboarding",
             "--description", "Genesis onboarding issue",
             "--color", "0E8A16"],
            cwd=path,
        )
    except GitHubError:
        pass

    issue_url = open_onboarding_issue(path)
    return repo_url
