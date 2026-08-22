"""GitHub integration for genesis.

Handles repo creation, pushing, and issue management via the gh CLI.
"""

import json
import subprocess
import time
import urllib.parse
from pathlib import Path

from genesis.scaffold import SEED_WORKFLOWS


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


def ssh_remote_url(repo_url: str) -> str:
    """The SSH form of a GitHub repo URL, for use as a git remote.

    Every repo genesis creates gets an SSH `origin`, never HTTPS, and the reason
    is specific rather than stylistic. An HTTPS remote authenticates through
    git's credential helper, which on a developer machine is usually
    `gh auth git-credential` - so the push borrows whichever `gh` account happens
    to be ACTIVE, not the one that owns the repo. With several accounts logged in
    (a work one and a personal one is the common case) that push fails with
    `Permission to <org>/<repo>.git denied to <wrong-account>`, and it fails at the
    end of a run rather than at setup. Worse, when the wrong account *does* have
    write access, the push silently lands under the wrong identity.

    An SSH remote is pinned to the key on disk, so it behaves the same whatever
    `gh` is doing. Measured on `Sayfan-AI/MaKlaude`, whose HTTPS origin (created by
    this function) denied a push until the active account was switched by hand.

    Idempotent, and accepts either form as input so it can normalise an existing
    remote as readily as a fresh URL.
    """
    if repo_url.startswith("git@"):
        return repo_url if repo_url.endswith(".git") else f"{repo_url}.git"

    parsed = urllib.parse.urlparse(repo_url)
    host = parsed.netloc or "github.com"
    path = parsed.path.strip("/")
    if not path:
        raise GitHubError(f"cannot derive an SSH remote from {repo_url!r}")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return f"git@{host}:{path}.git"


def create_github_repo(
    project_name: str,
    org: str | None = None,
    private: bool = True,
) -> str:
    """Create a new GitHub repo. Returns the browsable HTTPS URL.

    HTTPS on purpose: this value is shown to a person and pasted into issues, so
    it needs to be clickable. The git remote is derived from it by
    [ssh_remote_url] at the point it is used, which is the only place the
    distinction matters.
    """
    repo_name = f"{org}/{project_name}" if org else project_name
    args = ["repo", "create", repo_name]
    if private:
        args.append("--private")
    else:
        args.append("--public")

    _run_gh(args)

    # Get the repo URL
    owner = org if org else _run_gh(["api", "user", "--jq", ".login"])
    return f"https://github.com/{owner}/{project_name}"


def push_to_github(repo_path: Path, repo_url: str) -> None:
    """Point `origin` at the repo over SSH and push main. See [ssh_remote_url]."""
    ssh_url = ssh_remote_url(repo_url)

    try:
        existing = _run_git(repo_path, "remote", "get-url", "origin")
    except GitHubError:
        existing = ""

    if not existing:
        _run_git(repo_path, "remote", "add", "origin", ssh_url)
    elif ssh_remote_url(existing) != ssh_url:
        # A remote pointing at a DIFFERENT repo is a conflict, not something to
        # overwrite. Silently repointing it would push this project's history at
        # somebody else's repo, which is the one outcome here worth refusing.
        raise GitHubError(
            f"origin already points at {existing!r}, not {ssh_url!r}; "
            "remove or fix the remote before publishing"
        )
    elif existing != ssh_url:
        # Same repo, wrong transport - an HTTPS origin from an older genesis, or
        # from a re-run of this flow before the SSH change. Upgrade it in place so
        # the end state does not depend on when the repo was scaffolded.
        _run_git(repo_path, "remote", "set-url", "origin", ssh_url)

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


def disable_seed_workflows(repo_path: Path, timeout: float = 30.0) -> list[str]:
    """Disable the freshly-seeded workflows so they don't run before secrets exist.

    A dev system's workflows authenticate as the Genesis App and call the Anthropic
    API. Until the human installs the App and sets the secrets, every trigger would
    just fail. So genesis disables them at publish time; the human re-enables them
    with `.genesis/scripts/activate.sh` once the credentials are in place.

    Right after the first push GitHub needs a moment to register the workflow files,
    so we poll (inferring the repo from the clone's origin remote via ``cwd``) until
    all seed workflows are visible or ``timeout`` elapses, then disable each active
    one by ID. Returns the names of the workflows that were disabled.
    """
    deadline = time.monotonic() + timeout
    workflows = _list_workflows(repo_path)
    while len(workflows) < len(SEED_WORKFLOWS) and time.monotonic() < deadline:
        time.sleep(2)
        workflows = _list_workflows(repo_path)

    disabled: list[str] = []
    for wf in workflows:
        if wf.get("state") != "active":
            continue
        _run_gh(["workflow", "disable", str(wf["id"])], cwd=repo_path)
        disabled.append(wf["name"])
    return disabled


def _list_workflows(repo_path: Path) -> list[dict]:
    """List all workflows in the repo backing ``repo_path`` (via its origin remote)."""
    out = _run_gh(
        ["workflow", "list", "--all", "--json", "id,name,state"],
        cwd=repo_path,
    )
    return json.loads(out) if out else []


def publish_to_github(
    path: Path,
    project_name: str,
    goal: str,
    org: str | None = None,
    private: bool = True,
) -> str:
    """Full publish flow: create repo, push, disable workflows, open issue #1.

    Returns the repo URL.
    """
    # Ensure the branch is named main
    try:
        _run_git(path, "branch", "-M", "main")
    except GitHubError:
        pass  # Already on main

    repo_url = create_github_repo(project_name, org=org, private=private)
    push_to_github(path, repo_url)

    # Disable the seed workflows until the human supplies credentials (see above).
    disable_seed_workflows(path)

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
