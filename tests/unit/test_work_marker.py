"""What counts as "this session changed the repo" (#47).

The continuation ladder resumes a session that appears to be making progress, so
the definition of progress is what bounds the spend. It used to be "the repository
looks different", which is the same thing only when the session is the sole writer
- and it never is. These tests pin the difference against a real git repo, because
the distinctions being drawn (pulled vs committed, tracked vs untracked) are
distinctions git makes and a mock would only restate.
"""

from __future__ import annotations

import subprocess

import pytest

from genesis import server


def _run(*args: str, cwd) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo with a real origin, cwd pointed at it."""
    work = tmp_path / "work"
    work.mkdir()
    origin = tmp_path / "origin.git"
    _run("git", "init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    _run("git", "init", "-q", "-b", "main", ".", cwd=work)
    _run("git", "config", "user.email", "dev@x", cwd=work)
    _run("git", "config", "user.name", "dev", cwd=work)
    _run("git", "remote", "add", "origin", str(origin), cwd=work)
    (work / "seed.txt").write_text("seed\n")
    _run("git", "add", "-A", cwd=work)
    _run("git", "commit", "-qm", "seed", cwd=work)
    _run("git", "push", "-q", "-u", "origin", "main", cwd=work)
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def outsider(repo, tmp_path):
    """Somebody else landing a commit on origin — a human merging a PR, say."""
    clone = tmp_path / "outsider"
    _run("git", "clone", "-q", str(tmp_path / "origin.git"), str(clone), cwd=tmp_path)
    _run("git", "config", "user.email", "them@x", cwd=clone)
    _run("git", "config", "user.name", "them", cwd=clone)

    def land(name: str = "theirs.txt") -> None:
        (clone / name).write_text("not ours\n")
        _run("git", "add", "-A", cwd=clone)
        _run("git", "commit", "-qm", f"outside commit {name}", cwd=clone)
        _run("git", "push", "-q", "origin", "main", cwd=clone)

    return land


def test_pulling_somebody_elses_merge_is_not_progress(repo, outsider) -> None:
    """The measured case: HEAD moves, and the session authored none of it."""
    before = server.session_work_marker()
    outsider()
    _run("git", "pull", "-q", "--ff-only", "origin", "main", cwd=repo)

    head_moved = server._git(["log", "--oneline", "-1"])
    assert "outside commit" in head_moved, "the pull has to have actually moved HEAD"
    assert server.session_work_marker() == before


def test_a_commit_this_repo_made_is_progress(repo) -> None:
    before = server.session_work_marker()
    (repo / "work.py").write_text("print('hi')\n")
    _run("git", "add", "-A", cwd=repo)
    _run("git", "commit", "-qm", "did the work", cwd=repo)

    assert server.session_work_marker() != before


def test_a_commit_survives_being_pushed(repo) -> None:
    """The reflog is what carries this.

    Reachability alone would lose it: once pushed, the commit is reachable from
    `refs/remotes/origin/main` and looks exactly like something that arrived from
    outside. Committing and pushing inside one attempt is the most productive
    shape a session has, so it's the one that must not read as idle.
    """
    before = server.session_work_marker()
    (repo / "work.py").write_text("print('hi')\n")
    _run("git", "add", "-A", cwd=repo)
    _run("git", "commit", "-qm", "did the work", cwd=repo)
    _run("git", "push", "-q", "origin", "main", cwd=repo)

    assert server.session_work_marker() != before


def test_editing_a_tracked_file_is_progress(repo) -> None:
    """Uncommitted work is still work — the resumed session finishes it."""
    before = server.session_work_marker()
    (repo / "seed.txt").write_text("edited\n")

    assert server.session_work_marker() != before


def test_a_stray_untracked_file_is_not_progress(repo) -> None:
    """A temporary file left by a tool and a new source file look identical by
    path, so this rung declines to guess and lets the judge, which is handed
    `git status --porcelain`, decide."""
    before = server.session_work_marker()
    (repo / "scratch.log").write_text("noise\n")

    assert server.session_work_marker() == before


def test_a_checkout_alone_is_not_progress(repo) -> None:
    """Branching is setup, not output, and it writes a reflog entry."""
    before = server.session_work_marker()
    _run("git", "checkout", "-q", "-b", "feature", cwd=repo)

    assert server.session_work_marker() == before
