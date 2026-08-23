"""Harness for chaos-testing the local control plane.

The dev system's interesting failures live in the Python control plane rather than
in the model: a session that dies mid-task, a chain that will not stop, a state
file that wedges the loop, a crash that strands a lock. None of those need a real
Claude session to reproduce, which is why this suite runs offline.

Two things are faked and nothing else. `claude` is replaced by a scripted
stand-in on PATH, so the real session plumbing runs (streaming, timeouts, process
groups, the continuation ladder) against a deterministic outcome. GitHub is
stubbed at the `gh`-and-events boundary, because the chaos under test is not
GitHub's behaviour.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from genesis import server

FAKE = Path(__file__).parent / "fake_claude.py"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway git repo laid out like a dev system, and cwd pointed at it.

    It has a real `origin` because the progress signal asks whether a commit is
    reachable from a remote-tracking ref, and a repo with no remote answers "no"
    for everything. Without an origin the outside-writer scenario cannot be
    written at all - somebody else's merge arriving over the wire is precisely
    what rung 3 used to score as this session's work (#47).
    """
    # The working repo is a *subdirectory* of tmp_path, not tmp_path itself, so
    # origin and the outsider clone have somewhere private to live. Putting them
    # in tmp_path.parent instead would share one origin across every test in the
    # session, and the second push would be rejected as non-fast-forward.
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    (work / ".genesis").mkdir()
    (work / ".genesis" / "config.toml").write_text('name = "chaos"\n')
    (work / ".claude" / "agents").mkdir(parents=True)
    (work / ".claude" / "agents" / "orchestrator.md").write_text("# orchestrator\n")
    subprocess.run(["git", "init", "-q", "-b", "main", "."], check=True)
    (work / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(
        ["git", "-c", "user.email=c@x", "-c", "user.name=c", "commit", "-qm", "seed"],
        check=True,
    )

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"], check=True)
    return work


@pytest.fixture
def outside_writer(repo, tmp_path):
    """Land a commit on origin that this repo did not author.

    A human merging a pull request while a session runs, or auto-merge landing a
    bot PR in the GitHub Actions mode. The session sees it only once it pulls.
    """
    clone = tmp_path / "outsider"

    def land(message: str = "somebody else's merge") -> None:
        if not clone.exists():
            subprocess.run(
                ["git", "clone", "-q", str(tmp_path / "origin.git"), str(clone)],
                check=True,
            )
        (clone / "outsider.txt").write_text(message + "\n")
        subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(clone), "-c", "user.email=o@x", "-c", "user.name=outsider",
             "commit", "-qm", message],
            check=True,
        )
        subprocess.run(["git", "-C", str(clone), "push", "-q", "origin", "main"], check=True)

    return land


@pytest.fixture
def script(repo, monkeypatch):
    """Install the fake claude on PATH and return a writer for its script."""
    bindir = repo / "fakebin"
    bindir.mkdir()
    shim = bindir / "claude"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    spec_path = repo / "claude-script.json"
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(spec_path))

    def write(sessions, judge="STOP"):
        spec_path.write_text(json.dumps({"sessions": sessions, "judge": judge}))
        return spec_path

    return write


@pytest.fixture
def plane(repo, monkeypatch):
    """A control plane with GitHub stubbed out, ready to run sessions."""
    monkeypatch.setattr(server, "_gh_token", lambda: "fake-token")
    monkeypatch.setattr(server, "mint_installation_token", lambda *a, **k: None)
    monkeypatch.setattr(server, "loki_push", lambda *a, **k: True)
    return server.LocalControlPlane(
        repo="chaos/repo", poll_interval=1, session_timeout=60,
        agent=".claude/agents/orchestrator.md",
    )


@pytest.fixture
def sessions_run(repo):
    """How many times the fake claude was invoked, judge calls included."""
    def count():
        counter = repo / "claude-script.json.n"
        return int(counter.read_text()) if counter.exists() else 0
    return count


@pytest.fixture
def issues_script(repo):
    """A stand-in for the seeded `issues.sh`, recording how the plane called it.

    Claim bookkeeping is the third thing the plane forks (after `claude` and
    `git`), and it runs on the path where a session has just died — which is the
    path this suite spends all its time on. Recorded rather than stubbed out, so
    a scenario can assert that a killed session actually handed its issue back.
    """
    script = repo / ".genesis" / "scripts" / "issues.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    record = repo / "issues-calls.txt"
    script.write_text(f'#!/bin/sh\necho "$GENESIS_SESSION|$*" >> "{record}"\n')

    def calls() -> list[str]:
        return record.read_text().splitlines() if record.exists() else []

    return calls
