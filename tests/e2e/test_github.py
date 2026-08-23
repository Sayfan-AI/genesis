"""Tests for GitHub integration (publish_to_github, open_onboarding_issue).

These tests mock subprocess calls to gh/git to avoid needing real GitHub access.
"""

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import json

from genesis.github import (
    GitHubError,
    create_github_repo,
    disable_seed_workflows,
    open_onboarding_issue,
    publish_to_github,
    push_to_github,
    ssh_remote_url,
)
from genesis.scaffold import SEED_WORKFLOWS


@patch("genesis.github.subprocess.run")
def test_create_github_repo_private(mock_run: MagicMock) -> None:
    # Mock gh repo create
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

    # Mock gh api user for getting username
    def side_effect(args: list[str], **kwargs: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        if "repo" in args and "create" in args:
            result.stdout = ""
        elif "api" in args and "user" in args:
            result.stdout = "testuser"
        return result

    mock_run.side_effect = side_effect

    url = create_github_repo("my-project", private=True)
    assert url == "https://github.com/testuser/my-project"

    # Verify gh repo create was called with --private
    first_call_args = mock_run.call_args_list[0][0][0]
    assert "repo" in first_call_args
    assert "create" in first_call_args
    assert "--private" in first_call_args


@patch("genesis.github.subprocess.run")
def test_create_github_repo_with_org(mock_run: MagicMock) -> None:
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

    url = create_github_repo("my-project", org="my-org", private=True)
    assert url == "https://github.com/my-org/my-project"

    # Verify org/project_name was passed
    first_call_args = mock_run.call_args_list[0][0][0]
    assert "my-org/my-project" in first_call_args


@patch("genesis.github.subprocess.run")
def test_push_to_github(mock_run: MagicMock) -> None:
    tmp = Path("/tmp/test-repo")

    # First call: git remote get-url fails (no remote yet)
    # Second call: git remote add origin succeeds
    # Third call: git push succeeds
    call_count = 0

    def side_effect(args: list[str], **kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            # git remote get-url origin -> fails
            result.returncode = 1
            result.stderr = "fatal: No such remote 'origin'"
            result.stdout = ""
        else:
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        return result

    mock_run.side_effect = side_effect
    push_to_github(tmp, "https://github.com/test/repo")

    # Verify remote add and push were called
    assert call_count == 3  # get-url, remote add, push

    # And that the remote it added is SSH, not the HTTPS URL it was handed.
    add_args = mock_run.call_args_list[1][0][0]
    assert "remote" in add_args and "add" in add_args
    assert "git@github.com:test/repo.git" in add_args, add_args


# ---------- SSH remotes (#51) ----------
#
# An HTTPS origin authenticates through git's credential helper, which is usually
# `gh auth git-credential`, so the push borrows whichever gh account is ACTIVE
# rather than the one that owns the repo. Measured on Sayfan-AI/MaKlaude: the push
# was denied to the wrong account until it was switched by hand.


@pytest.mark.parametrize(
    "given,want",
    [
        ("https://github.com/test/repo", "git@github.com:test/repo.git"),
        ("https://github.com/test/repo.git", "git@github.com:test/repo.git"),
        ("https://github.com/test/repo/", "git@github.com:test/repo.git"),
        # Already SSH: idempotent, so it can normalise an existing remote.
        ("git@github.com:test/repo.git", "git@github.com:test/repo.git"),
        ("git@github.com:test/repo", "git@github.com:test/repo.git"),
        # Not hardcoded to github.com, so an Enterprise host survives the trip.
        ("https://git.example.com/test/repo", "git@git.example.com:test/repo.git"),
    ],
)
def test_ssh_remote_url(given: str, want: str) -> None:
    assert ssh_remote_url(given) == want


def test_ssh_remote_url_rejects_a_url_with_no_repo_path() -> None:
    """Better to fail here than to hand git a remote of `git@github.com:.git`."""
    with pytest.raises(GitHubError):
        ssh_remote_url("https://github.com")


@patch("genesis.github.subprocess.run")
def test_push_upgrades_an_existing_https_origin_to_ssh(mock_run: MagicMock) -> None:
    """The end state must not depend on when the repo was scaffolded.

    A repo published by an older genesis has an HTTPS origin on disk. Re-running
    the publish flow should fix it rather than leave the defect in place.
    """
    calls: list[list[str]] = []

    def side_effect(args: list[str], **kwargs: object) -> MagicMock:
        calls.append(args)
        result = MagicMock(returncode=0, stderr="")
        result.stdout = "https://github.com/test/repo.git" if len(calls) == 1 else ""
        return result

    mock_run.side_effect = side_effect
    push_to_github(Path("/tmp/test-repo"), "https://github.com/test/repo")

    flat = [" ".join(c) for c in calls]
    assert any("set-url origin git@github.com:test/repo.git" in f for f in flat), flat
    assert not any("remote add" in f for f in flat), "origin existed; it must be repointed, not added"
    assert any("push -u origin main" in f for f in flat), flat


@patch("genesis.github.subprocess.run")
def test_push_leaves_a_correct_ssh_origin_alone(mock_run: MagicMock) -> None:
    """No redundant set-url on the happy path."""
    calls: list[list[str]] = []

    def side_effect(args: list[str], **kwargs: object) -> MagicMock:
        calls.append(args)
        result = MagicMock(returncode=0, stderr="")
        result.stdout = "git@github.com:test/repo.git" if len(calls) == 1 else ""
        return result

    mock_run.side_effect = side_effect
    push_to_github(Path("/tmp/test-repo"), "https://github.com/test/repo")

    flat = [" ".join(c) for c in calls]
    assert not any("set-url" in f for f in flat), flat
    assert not any("remote add" in f for f in flat), flat


@patch("genesis.github.subprocess.run")
def test_push_refuses_an_origin_pointing_at_a_different_repo(mock_run: MagicMock) -> None:
    """Repointing that silently would push this history at somebody else's repo."""
    def side_effect(args: list[str], **kwargs: object) -> MagicMock:
        result = MagicMock(returncode=0, stderr="")
        result.stdout = "git@github.com:someone/else.git"
        return result

    mock_run.side_effect = side_effect

    with pytest.raises(GitHubError, match="already points at"):
        push_to_github(Path("/tmp/test-repo"), "https://github.com/test/repo")


@patch("genesis.github.subprocess.run")
def test_open_onboarding_issue(mock_run: MagicMock, tmp_path: Path) -> None:
    # Create a fake onboarding file
    genesis_dir = tmp_path / ".genesis"
    genesis_dir.mkdir()
    onboarding = genesis_dir / "onboarding.md"
    onboarding.write_text("# Onboarding: my-project\n\n## Goal\n\nBuild something great\n")

    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="https://github.com/test/repo/issues/1",
        stderr="",
    )

    url = open_onboarding_issue(tmp_path)
    assert url == "https://github.com/test/repo/issues/1"

    # Verify gh issue create was called
    call_args = mock_run.call_args[0][0]
    assert "issue" in call_args
    assert "create" in call_args
    assert "--title" in call_args


def test_open_onboarding_issue_missing_file(tmp_path: Path) -> None:
    with pytest.raises(GitHubError, match="No onboarding file"):
        open_onboarding_issue(tmp_path)


@patch("genesis.github.subprocess.run")
def test_publish_to_github_full_flow(mock_run: MagicMock, tmp_path: Path) -> None:
    # Create a git repo with onboarding file
    genesis_dir = tmp_path / ".genesis"
    genesis_dir.mkdir()
    (genesis_dir / "onboarding.md").write_text("# Onboarding: test\n\nGoal here\n")

    call_idx = 0

    def side_effect(args: list[str], **kwargs: object) -> MagicMock:
        nonlocal call_idx
        call_idx += 1
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""

        args_list = list(args)

        if "branch" in args_list and "-M" in args_list:
            result.stdout = ""
        elif "repo" in args_list and "create" in args_list:
            result.stdout = ""
        elif "api" in args_list and "user" in args_list:
            result.stdout = "testuser"
        elif "remote" in args_list and "get-url" in args_list:
            result.returncode = 1
            result.stderr = "no remote"
            result.stdout = ""
        elif "remote" in args_list and "add" in args_list:
            result.stdout = ""
        elif "push" in args_list:
            result.stdout = ""
        elif "label" in args_list and "create" in args_list:
            result.stdout = ""
        elif "workflow" in args_list and "list" in args_list:
            # All seed workflows registered and active right after push.
            result.stdout = json.dumps(
                [
                    {"id": i, "name": wf, "state": "active"}
                    for i, wf in enumerate(SEED_WORKFLOWS)
                ]
            )
        elif "workflow" in args_list and "disable" in args_list:
            result.stdout = ""
        elif "issue" in args_list and "create" in args_list:
            result.stdout = "https://github.com/testuser/test/issues/1"
        else:
            result.stdout = ""

        return result

    mock_run.side_effect = side_effect

    url = publish_to_github(tmp_path, "test", "Build something", private=True)
    assert url == "https://github.com/testuser/test"

    # Every seed workflow should have been disabled during publish.
    disable_calls = [
        c for c in mock_run.call_args_list
        if "workflow" in c[0][0] and "disable" in c[0][0]
    ]
    assert len(disable_calls) == len(SEED_WORKFLOWS)


@patch("genesis.github.time.sleep")
@patch("genesis.github.subprocess.run")
def test_disable_seed_workflows_disables_only_active(
    mock_run: MagicMock, mock_sleep: MagicMock, tmp_path: Path
) -> None:
    # Derived from SEED_WORKFLOWS, not hand-written. `disable_seed_workflows`
    # polls until the listing is at least as long as the seed manifest, so a
    # fixture that falls one workflow short stops testing the disable logic and
    # starts testing the timeout loop instead. Seeding genesis-merge.yml is
    # exactly what broke the hard-coded version.
    seeded = [
        {
            "id": 10 + i,
            "name": name.removeprefix("genesis-").removesuffix(".yml"),
            "state": "active",
        }
        for i, name in enumerate(SEED_WORKFLOWS)
    ]
    seeded[-1]["state"] = "disabled_manually"
    listing = json.dumps(seeded)

    def side_effect(args: list[str], **kwargs: object) -> MagicMock:
        result = MagicMock(returncode=0, stderr="")
        args_list = list(args)
        if "workflow" in args_list and "list" in args_list:
            result.stdout = listing
        else:
            result.stdout = ""
        return result

    mock_run.side_effect = side_effect

    disabled = disable_seed_workflows(tmp_path)

    # The already-disabled one is left alone; every active one gets disabled.
    assert disabled == [wf["name"] for wf in seeded[:-1]]
    disable_ids = [
        c[0][0][c[0][0].index("disable") + 1]
        for c in mock_run.call_args_list
        if "workflow" in c[0][0] and "disable" in c[0][0]
    ]
    assert disable_ids == [str(wf["id"]) for wf in seeded[:-1]]
    mock_sleep.assert_not_called()  # listing was complete on first poll
